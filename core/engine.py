"""
Core Execution Engine
=====================
Owns the main async loop: tick → fetch prices → detect opportunity →
validate risk → execute (or skip) → log.

Phase 3 additions:
    • Multi-strategy support (spatial + triangular, selectable via config)
    • WebSocket streaming mode (reads from in-memory cache instead of REST)
    • Health monitor integration (exposes /health endpoint)
    • Concurrent execution routing
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Dict, List, Optional

from core.executor import Executor
from core.health import HealthMonitor
from exchanges.base import BaseExchangeAdapter, OrderBookSnapshot
from exchanges.factory import create_adapters
from exchanges.ws_manager import WebSocketManager
from risk.manager import RiskManager
from strategies.base import BaseStrategy, Opportunity
from strategies.spatial import SpatialConfig, SpatialStrategy
from strategies.triangular import TriangularConfig, TriangularStrategy
from utils.logger import get_logger

if TYPE_CHECKING:
    from config.settings import Settings

logger = get_logger(__name__)


class Engine:
    """
    The deterministic heartbeat of ArbCore.

    Lifecycle
    ---------
    1.  ``start()`` — connects exchanges, launches WS/health, enters loop.
    2.  Each *tick*:
            a. Read snapshots (from WS cache or REST fetch).
            b. Run all active strategies.
            c. Gate opportunities through risk.
            d. Route approved ones to the executor.
    3.  ``request_shutdown()`` → clean exit on next iteration.
    4.  ``shutdown()`` → close WS streams, exchange connections, health server.
    """

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._running = False
        self._shutdown_requested = False

        # Initialised in start()
        self._adapters: Dict[str, BaseExchangeAdapter] = {}
        self._strategies: List[BaseStrategy] = []
        self._risk: Optional[RiskManager] = None
        self._executor: Optional[Executor] = None
        self._ws_manager: Optional[WebSocketManager] = None
        self._health: Optional[HealthMonitor] = None
        self._symbols: List[str] = []

        # Fee cache: {exchange: {symbol: taker_fee}}
        self._fee_cache: Dict[str, Dict[str, float]] = {}

        # Stats
        self._tick_count: int = 0
        self._opportunities_found: int = 0
        self._trades_executed: int = 0
        self._session_pnl: float = 0.0

    def request_shutdown(self) -> None:
        """Signal the engine to exit after the current tick completes."""
        self._shutdown_requested = True

    async def start(self) -> None:
        """Connect exchanges, launch services, and enter the tick loop."""

        # ── 1. Health monitor (start first so Railway sees a port) ──
        self._health = HealthMonitor(port=self._settings.health_port)
        try:
            await self._health.start()
        except Exception:
            logger.warning("Health monitor failed to start — continuing without it.")
            self._health = None

        # ── 2. Connect exchanges ─────────────────────────
        logger.info("Connecting to exchanges: %s", self._settings.enabled_exchanges)
        try:
            self._adapters = await create_adapters(self._settings)
        except Exception:
            logger.exception("Failed to connect to exchanges — aborting.")
            return

        # ── 3. Resolve tradeable symbols ─────────────────
        self._symbols = self._resolve_common_symbols()
        if not self._symbols:
            logger.error("No common tradeable symbols found — aborting.")
            return
        logger.info("Monitoring %d symbols: %s", len(self._symbols), self._symbols[:10])

        # ── 4. Build strategies ──────────────────────────
        self._strategies = self._build_strategies()
        strategy_names = [s.name for s in self._strategies]
        logger.info("Active strategies: %s", strategy_names)

        # ── 5. Build risk manager ────────────────────────
        self._risk = RiskManager(self._settings.risk)

        # ── 6. Build executor ────────────────────────────
        self._executor = Executor(
            adapters=self._adapters,
            mode=self._settings.mode,
            concurrent=self._settings.concurrent_execution,
        )

        # ── 7. Start WebSocket streams (if enabled) ──────
        if self._settings.use_websocket:
            self._ws_manager = WebSocketManager(
                adapters=self._adapters,
                symbols=self._symbols,
            )
            await self._ws_manager.start()
            # Give streams a moment to connect
            await asyncio.sleep(2.0)
            logger.info(
                "WebSocket mode active — %d streams connected",
                self._ws_manager.stream_count,
            )

        # ── 8. Update health stats ───────────────────────
        if self._health:
            self._health.update_stats(
                mode=self._settings.mode,
                exchanges=self._settings.enabled_exchanges,
                symbols_monitored=len(self._symbols),
                strategies=strategy_names,
            )

        # ── 9. Main loop ────────────────────────────────
        self._running = True
        logger.info(
            "Engine started — mode=%s | poll=%.2fs | ws=%s | concurrent=%s",
            self._settings.mode,
            self._settings.poll_interval_s,
            self._settings.use_websocket,
            self._settings.concurrent_execution,
        )

        while not self._shutdown_requested:
            tick_start = time.monotonic()
            try:
                await self._tick()
            except Exception:
                logger.exception("Unhandled exception during tick — continuing")

            elapsed = time.monotonic() - tick_start
            sleep_time = max(0, self._settings.poll_interval_s - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        self._running = False
        logger.info(
            "Engine loop exited — ticks=%d | opps=%d | trades=%d | pnl=$%.4f",
            self._tick_count, self._opportunities_found,
            self._trades_executed, self._session_pnl,
        )

    async def _tick(self) -> None:
        """Single iteration of the scan-evaluate-execute cycle."""
        self._tick_count += 1
        tick_opps = 0
        tick_trades = 0
        tick_pnl = 0.0

        # ── 1. Get snapshots ─────────────────────────────
        if self._ws_manager:
            snapshots = self._ws_manager.get_all_snapshots()
        else:
            snapshots = await self._fetch_all_books()

        if not snapshots:
            return

        # ── 2. Get fees (cached) ─────────────────────────
        fee_schedule = await self._fetch_all_fees()

        # ── 3. Run all active strategies ─────────────────
        all_opportunities: List[Opportunity] = []
        for strategy in self._strategies:
            try:
                opps = strategy.scan(snapshots, fee_schedule)
                all_opportunities.extend(opps)
            except Exception:
                logger.exception("Strategy %s crashed during scan", strategy.name)

        # Sort globally by net profitability
        all_opportunities.sort(key=lambda o: o.net_pct, reverse=True)

        if all_opportunities:
            tick_opps = len(all_opportunities)
            self._opportunities_found += tick_opps
            logger.info(
                "Tick #%d — %d opportunity(ies): %s",
                self._tick_count, tick_opps,
                ", ".join(f"{o.strategy}:{o.net_pct:.3f}%" for o in all_opportunities[:5]),
            )

        # ── 4. Risk gate → Execute ───────────────────────
        for opp in all_opportunities:
            verdict = self._risk.evaluate(opp, self._settings.mode)

            if not verdict.approved:
                logger.debug(
                    "RISK_REJECT | %s | net=%.4f%% | %s",
                    opp.strategy, opp.net_pct, verdict.reason,
                )
                continue

            report = await self._executor.execute(opp)
            tick_trades += 1
            self._trades_executed += 1
            tick_pnl += report.net_pnl_usd
            self._session_pnl += report.net_pnl_usd

            # Feed P&L back to risk manager
            notional = sum(l.price * l.quantity for l in opp.legs)
            self._risk.record_fill(report.net_pnl_usd, notional)

        # ── 5. Update health ─────────────────────────────
        if self._health:
            ws_streams = self._ws_manager.stream_count if self._ws_manager else 0
            self._health.record_tick(
                opportunities=tick_opps,
                trades=tick_trades,
                pnl_delta=tick_pnl,
            )
            self._health.update_stats(ws_streams_active=ws_streams)

    # ── Strategy builder ─────────────────────────────────

    def _build_strategies(self) -> List[BaseStrategy]:
        """Instantiate strategies based on config."""
        strategies: List[BaseStrategy] = []
        cfg = self._settings

        if cfg.strategy in ("spatial", "both"):
            strategies.append(SpatialStrategy(SpatialConfig(
                symbols=self._symbols,
                min_net_spread_pct=0.05,
                max_trade_qty_usd=cfg.risk.max_trade_usd,
            )))

        if cfg.strategy in ("triangular", "both"):
            strategies.append(TriangularStrategy(TriangularConfig(
                base_currency="USDT",
                min_net_spread_pct=0.05,
                max_trade_qty_usd=cfg.risk.max_trade_usd,
            )))

        return strategies

    # ── Data fetching ────────────────────────────────────

    async def _fetch_all_books(
        self,
    ) -> Dict[str, Dict[str, OrderBookSnapshot]]:
        """Fetch order-book snapshots via REST (fallback when WS is off)."""
        result: Dict[str, Dict[str, OrderBookSnapshot]] = {}

        async def _fetch_one(ex_name: str, symbol: str):
            adapter = self._adapters[ex_name]
            try:
                return ex_name, symbol, await adapter.fetch_order_book(symbol)
            except Exception as exc:
                logger.warning("Book fetch failed: %s/%s — %s", ex_name, symbol, exc)
                return ex_name, symbol, None

        tasks = [
            _fetch_one(ex, sym)
            for ex in self._adapters
            for sym in self._symbols
        ]

        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for item in completed:
            if isinstance(item, Exception):
                logger.warning("gather exception: %s", item)
                continue
            ex_name, symbol, snapshot = item
            if snapshot is None:
                continue
            result.setdefault(ex_name, {})[symbol] = snapshot

        return result

    async def _fetch_all_fees(self) -> Dict[str, Dict[str, float]]:
        """Fetch taker fees (cached for session lifetime)."""
        for ex_name, adapter in self._adapters.items():
            if ex_name not in self._fee_cache:
                self._fee_cache[ex_name] = {}
            for sym in self._symbols:
                if sym not in self._fee_cache[ex_name]:
                    try:
                        fee = await adapter.fetch_trading_fee(sym)
                        self._fee_cache[ex_name][sym] = fee
                    except Exception:
                        self._fee_cache[ex_name][sym] = 0.001

        return self._fee_cache

    def _resolve_common_symbols(self) -> List[str]:
        """Find symbols tradeable on ALL connected exchanges."""
        watchlist = [
            "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
            "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT",
            "MATIC/USDT", "LINK/USDT", "LTC/USDT", "UNI/USDT",
            "ETH/BTC", "SOL/BTC", "XRP/BTC", "SOL/ETH",
        ]

        common: List[str] = []
        for sym in watchlist:
            if all(
                self._adapters[ex]._client is not None
                and sym in (self._adapters[ex]._client.markets or {})
                for ex in self._adapters
            ):
                common.append(sym)

        return common

    async def shutdown(self) -> None:
        """Graceful teardown of all services."""
        logger.info("Engine shutdown sequence starting …")

        if self._ws_manager:
            await self._ws_manager.stop()

        for name, adapter in self._adapters.items():
            try:
                await adapter.close()
            except Exception:
                logger.exception("Error closing %s", name)

        if self._health:
            await self._health.stop()

        logger.info("Engine shutdown complete.")
