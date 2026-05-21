"""
Core Execution Engine
=====================
Owns the main async loop: tick → fetch prices → detect opportunity →
validate risk → execute (or skip) → log.

Phase 4 additions:
    • Circuit breaker — hard kill-switch on API freeze / WS death
    • Oracle CI filter — Pyth Network stale-quote confirmation
    • Feed reconciler — REST vs WS book integrity checking
    • Rate limiters — per-exchange token-bucket throttling
    • Latency tracker — execution histogram for ALP detection
    • Dynamic fee refresh — periodic re-fetch of taker fees
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from core.executor import Executor
from core.health import HealthMonitor
from data.oracle import OracleClient
from data.feed_reconciler import FeedReconciler
from exchanges.base import BaseExchangeAdapter, OrderBookSnapshot
from exchanges.factory import create_adapters
from exchanges.ws_manager import WebSocketManager
from risk.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from risk.manager import RiskManager
from risk.rate_limiter import create_rate_limiters
from strategies.base import BaseStrategy, Opportunity
from strategies.filters import filter_by_feed_health, filter_by_oracle_ci
from strategies.spatial import SpatialConfig, SpatialStrategy
from strategies.triangular import TriangularConfig, TriangularStrategy
from utils.latency_tracker import LatencyTracker
from utils.logger import get_logger

if TYPE_CHECKING:
    from config.settings import Settings

logger = get_logger(__name__)


class Engine:
    """
    The deterministic heartbeat of ArbCore.

    Lifecycle
    ---------
    1.  ``start()`` — connects exchanges, launches services, enters loop.
    2.  Each *tick*:
            a. Circuit breaker gate (Phase 4).
            b. Read snapshots (from WS cache or REST fetch).
            c. Run all active strategies.
            d. Apply oracle CI + feed health filters (Phase 4).
            e. Gate opportunities through risk.
            f. Route approved ones to the executor.
            g. Feed latency data to tracker + breaker (Phase 4).
    3.  ``request_shutdown()`` → clean exit on next iteration.
    4.  ``shutdown()`` → close all services.
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

        # Phase 4: New subsystems
        self._circuit_breaker: Optional[CircuitBreaker] = None
        self._oracle: Optional[OracleClient] = None
        self._reconciler: Optional[FeedReconciler] = None
        self._latency_tracker: Optional[LatencyTracker] = None
        self._rate_limiters: Dict[str, Any] = {}

        # Fee cache: {exchange: {symbol: taker_fee}}
        self._fee_cache: Dict[str, Dict[str, float]] = {}
        self._fee_cache_time: float = 0.0

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

        # ── 7. Phase 4: Circuit breaker ──────────────────
        self._circuit_breaker = CircuitBreaker(CircuitBreakerConfig(
            max_consecutive_api_failures=self._settings.risk.max_consecutive_api_failures,
            max_order_latency_ms=self._settings.risk.max_order_latency_ms,
            max_ws_silence_s=self._settings.risk.max_ws_silence_s,
            max_unhedged_hold_s=self._settings.risk.max_unhedged_hold_s,
            cooldown_s=self._settings.risk.circuit_breaker_cooldown_s,
        ))
        logger.info("Circuit breaker armed.")

        # ── 8. Phase 4: Rate limiters ────────────────────
        self._rate_limiters = create_rate_limiters(self._settings.enabled_exchanges)

        # ── 9. Phase 4: Latency tracker ──────────────────
        self._latency_tracker = LatencyTracker()

        # ── 10. Start WebSocket streams (if enabled) ─────
        if self._settings.use_websocket:
            self._ws_manager = WebSocketManager(
                adapters=self._adapters,
                symbols=self._symbols,
            )
            await self._ws_manager.start()
            await asyncio.sleep(2.0)
            logger.info(
                "WebSocket mode active — %d streams connected",
                self._ws_manager.stream_count,
            )

        # ── 11. Phase 4: Oracle (if enabled) ─────────────
        if self._settings.use_oracle:
            self._oracle = OracleClient(
                max_ci_pct=self._settings.oracle_max_ci_pct,
                refresh_interval_s=self._settings.oracle_refresh_interval_s,
            )
            try:
                await self._oracle.start()
                logger.info(
                    "Oracle integration active — Pyth CI filter enabled "
                    "(max_ci=%.1f%%, refresh=%.1fs)",
                    self._settings.oracle_max_ci_pct,
                    self._settings.oracle_refresh_interval_s,
                )
            except Exception:
                logger.warning("Oracle client failed to start — continuing without it.")
                self._oracle = None

        # ── 12. Phase 4: Feed reconciler (WS mode only) ──
        if self._ws_manager:
            self._reconciler = FeedReconciler(
                adapters=self._adapters,
                ws_manager=self._ws_manager,
                symbols=self._symbols,
                interval_s=self._settings.feed_reconcile_interval_s,
                divergence_threshold_pct=self._settings.feed_desync_threshold_pct,
            )
            await self._reconciler.start()

        # ── 13. Update health stats ──────────────────────
        if self._health:
            self._health.update_stats(
                mode=self._settings.mode,
                exchanges=self._settings.enabled_exchanges,
                symbols_monitored=len(self._symbols),
                strategies=strategy_names,
                oracle_active=self._oracle is not None,
                circuit_breaker="armed",
            )

        # ── 14. Main loop ───────────────────────────────
        self._running = True
        logger.info(
            "Engine started — mode=%s | poll=%.2fs | ws=%s | concurrent=%s | oracle=%s",
            self._settings.mode,
            self._settings.poll_interval_s,
            self._settings.use_websocket,
            self._settings.concurrent_execution,
            self._oracle is not None,
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

        # ── 0. Phase 4: Circuit breaker gate ─────────────
        if self._circuit_breaker:
            if self._reconciler:
                self._circuit_breaker.update_desync_count(
                    len(self._reconciler.desynced_feeds)
                )
            trip_reason = self._circuit_breaker.check()
            if trip_reason:
                logger.critical(
                    "CIRCUIT BREAKER ACTIVE — halting engine: %s", trip_reason,
                )
                self._shutdown_requested = True
                return

        # ── 1. Get snapshots ─────────────────────────────
        if self._ws_manager:
            snapshots = self._ws_manager.get_all_snapshots()
            # Phase 4: Signal WS health to circuit breaker
            if self._circuit_breaker and snapshots:
                self._circuit_breaker.record_ws_data()
        else:
            snapshots = await self._fetch_all_books()

        if not snapshots:
            return

        # ── 2. Get fees (cached with TTL refresh) ────────
        fee_schedule = await self._fetch_all_fees()

        # ── 3. Run all active strategies ─────────────────
        all_opportunities: List[Opportunity] = []
        for strategy in self._strategies:
            try:
                opps = strategy.scan(snapshots, fee_schedule)
                all_opportunities.extend(opps)
            except Exception:
                logger.exception("Strategy %s crashed during scan", strategy.name)

        # ── 3b. Phase 4: Apply oracle CI + feed health filters ──
        pre_filter_count = len(all_opportunities)
        all_opportunities = filter_by_oracle_ci(all_opportunities, self._oracle)
        if self._reconciler:
            all_opportunities = filter_by_feed_health(
                all_opportunities, self._reconciler.desynced_feeds
            )

        if pre_filter_count > 0 and len(all_opportunities) < pre_filter_count:
            logger.info(
                "Phase 4 filters: %d → %d opportunities (rejected %d)",
                pre_filter_count,
                len(all_opportunities),
                pre_filter_count - len(all_opportunities),
            )

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

            # ── 4b. Phase 4: Feed data to circuit breaker + tracker ──
            if self._circuit_breaker:
                for fill in report.fills:
                    if fill.success:
                        self._circuit_breaker.record_api_success()
                        self._circuit_breaker.record_order_latency(fill.latency_ms)
                    else:
                        self._circuit_breaker.record_api_failure()
                if report.unhedged:
                    self._circuit_breaker.record_unhedged_open()
                elif not report.unhedged:
                    self._circuit_breaker.record_unhedged_closed()

            if self._latency_tracker:
                for fill in report.fills:
                    if fill.success:
                        self._latency_tracker.record(fill.exchange, fill.latency_ms)

        # ── 5. Update health ─────────────────────────────
        if self._health:
            ws_streams = self._ws_manager.stream_count if self._ws_manager else 0
            self._health.record_tick(
                opportunities=tick_opps,
                trades=tick_trades,
                pnl_delta=tick_pnl,
            )
            extra_stats: dict = {"ws_streams_active": ws_streams}
            if self._circuit_breaker:
                extra_stats["circuit_breaker"] = (
                    "tripped" if self._circuit_breaker.is_tripped else "armed"
                )
            if self._oracle:
                extra_stats["oracle_feeds"] = self._oracle.feed_count
            if self._reconciler:
                extra_stats["desynced_feeds"] = len(self._reconciler.desynced_feeds)
            self._health.update_stats(**extra_stats)

        # ── 6. Phase 4: Periodic ALP detection log ───────
        if self._latency_tracker and self._tick_count % 100 == 0:
            for ex_name in self._adapters:
                stats = self._latency_tracker.get_stats(ex_name)
                if stats and stats.suspected_floor_ms is not None:
                    logger.warning(
                        "ALP_ALERT | %s | suspected floor at %.0fms "
                        "(mean=%.0fms median=%.0fms p95=%.0fms)",
                        ex_name,
                        stats.suspected_floor_ms,
                        stats.mean_ms,
                        stats.median_ms,
                        stats.p95_ms,
                    )

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
                # Phase 4: record failure to circuit breaker
                if self._circuit_breaker:
                    self._circuit_breaker.record_api_failure()
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
            # Phase 4: record success
            if self._circuit_breaker:
                self._circuit_breaker.record_api_success()

        return result

    async def _fetch_all_fees(self) -> Dict[str, Dict[str, float]]:
        """
        Fetch taker fees with TTL-based cache refresh.

        Phase 4: fees are refreshed periodically instead of cached forever,
        since Tier-2 exchanges can change fee tiers without notice.
        """
        now = time.monotonic()
        cache_expired = (
            now - self._fee_cache_time > self._settings.fee_refresh_interval_s
        )

        for ex_name, adapter in self._adapters.items():
            if ex_name not in self._fee_cache:
                self._fee_cache[ex_name] = {}
            for sym in self._symbols:
                if sym not in self._fee_cache[ex_name] or cache_expired:
                    try:
                        fee = await adapter.fetch_trading_fee(sym)
                        self._fee_cache[ex_name][sym] = fee
                    except Exception:
                        self._fee_cache[ex_name].setdefault(sym, 0.001)

        if cache_expired:
            self._fee_cache_time = now
            logger.debug("Fee cache refreshed for all exchanges.")

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

        # Phase 4: Stop new subsystems
        if self._oracle:
            await self._oracle.stop()

        if self._reconciler:
            await self._reconciler.stop()

        if self._ws_manager:
            await self._ws_manager.stop()

        for name, adapter in self._adapters.items():
            try:
                await adapter.close()
            except Exception:
                logger.exception("Error closing %s", name)

        if self._health:
            await self._health.stop()

        # Phase 4: Log final latency stats
        if self._latency_tracker:
            for ex_name, stats in self._latency_tracker.get_all_stats().items():
                logger.info(
                    "LATENCY_SUMMARY | %s | n=%d mean=%.0fms median=%.0fms "
                    "p95=%.0fms p99=%.0fms floor=%s",
                    ex_name, stats.count, stats.mean_ms, stats.median_ms,
                    stats.p95_ms, stats.p99_ms,
                    f"{stats.suspected_floor_ms:.0f}ms" if stats.suspected_floor_ms else "none",
                )

        # Phase 4: Log circuit breaker history
        if self._circuit_breaker and self._circuit_breaker.trip_history:
            logger.warning(
                "Circuit breaker tripped %d time(s) during session.",
                len(self._circuit_breaker.trip_history),
            )

        logger.info("Engine shutdown complete.")
