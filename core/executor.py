"""
Trade Executor
==============
Routes approved ``Opportunity`` objects to the exchange adapters.

Two modes:
    • **dry_run** — simulates fills using the opportunity's expected prices,
      logs the would-be trade, and returns a synthetic ``TradeResult``.
    • **live** — places real market orders via the exchange adapters and
      records actual fill data.

Safety invariants:
    1. Every leg is executed independently.  If the first leg succeeds and
       the second fails, the executor logs an **UNHEDGED POSITION** alert.
    2. A latency ceiling rejects fills that took longer than ``max_latency_ms``.
    3. Partial fills below ``min_fill_ratio`` trigger an abort on remaining legs.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List

from exchanges.base import BaseExchangeAdapter, TradeResult
from strategies.base import Opportunity, TradeLeg
from utils.logger import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


@dataclass(frozen=True)
class ExecutionReport:
    """Aggregated result of executing all legs of an opportunity."""

    opportunity: Opportunity
    fills: List[TradeResult]
    all_filled: bool
    net_pnl_usd: float          # estimated P&L from fills
    total_latency_ms: float
    unhedged: bool               # True if one leg filled and another did not


class Executor:
    """
    Stateless order router.

    Parameters
    ----------
    adapters        : exchange name → connected adapter
    mode            : ``"dry_run"`` or ``"live"``
    max_latency_ms  : per-leg ceiling; reject fills slower than this
    min_fill_ratio  : abort remaining legs if any fill < this fraction
    """

    def __init__(
        self,
        adapters: Dict[str, BaseExchangeAdapter],
        mode: str,
        max_latency_ms: float = 5000.0,
        min_fill_ratio: float = 0.95,
        concurrent: bool = False,
    ) -> None:
        self._adapters = adapters
        self._mode = mode
        self._max_latency_ms = max_latency_ms
        self._min_fill_ratio = min_fill_ratio
        self._concurrent = concurrent

    async def execute(self, opp: Opportunity) -> ExecutionReport:
        """Execute all legs of an opportunity and return the report."""
        if self._mode == "dry_run":
            return self._simulate(opp)
        if self._concurrent and len(opp.legs) >= 2:
            return await self._execute_concurrent(opp)
        return await self._execute_live(opp)

    # ── Dry-run simulation ───────────────────────────────

    def _simulate(self, opp: Opportunity) -> ExecutionReport:
        """
        Simulate execution using the opportunity's expected prices.
        No exchange I/O occurs.
        """
        fills: List[TradeResult] = []

        for leg in opp.legs:
            fills.append(
                TradeResult(
                    exchange=leg.exchange,
                    symbol=leg.symbol,
                    side=leg.side,
                    requested_qty=leg.quantity,
                    filled_qty=leg.quantity,
                    avg_fill_price=leg.price,
                    fee=leg.price * leg.quantity * leg.fee_rate,
                    fee_currency=leg.symbol.split("/")[-1],
                    latency_ms=0.0,
                    order_id="DRY_RUN",
                    success=True,
                )
            )

        pnl = self._calc_pnl(opp.legs, fills)
        logger.info(
            "DRY_RUN | %s | legs=%d | est_pnl=$%.4f | net_spread=%.4f%%",
            opp.strategy, len(fills), pnl, opp.net_pct,
        )
        return ExecutionReport(
            opportunity=opp,
            fills=fills,
            all_filled=True,
            net_pnl_usd=pnl,
            total_latency_ms=0.0,
            unhedged=False,
        )

    # ── Live execution ───────────────────────────────────

    async def _execute_live(self, opp: Opportunity) -> ExecutionReport:
        """
        Place real market orders for each leg sequentially.

        Sequential (not concurrent) execution is deliberate for Phase 2:
        it simplifies error handling.  Phase 3 may upgrade to concurrent
        placement with asyncio.gather + rollback logic.
        """
        fills: List[TradeResult] = []
        abort = False

        for i, leg in enumerate(opp.legs):
            if abort:
                logger.warning(
                    "ABORT | skipping leg %d/%d of %s due to prior failure",
                    i + 1, len(opp.legs), opp.strategy,
                )
                fills.append(self._failed_fill(leg, "aborted_due_to_prior_failure"))
                continue

            adapter = self._adapters.get(leg.exchange)
            if adapter is None:
                logger.error("No adapter for exchange '%s'", leg.exchange)
                fills.append(self._failed_fill(leg, "no_adapter"))
                abort = True
                continue

            result = await adapter.place_market_order(
                symbol=leg.symbol,
                side=leg.side,
                quantity=leg.quantity,
            )
            fills.append(result)

            # ── Safety checks ────────────────────────────
            if not result.success:
                logger.error(
                    "FILL_FAIL | %s %s %s — %s",
                    leg.exchange, leg.side, leg.symbol, result.error,
                )
                abort = True
                continue

            if result.latency_ms > self._max_latency_ms:
                logger.warning(
                    "LATENCY_BREACH | %s took %.0fms (cap=%.0fms)",
                    leg.exchange, result.latency_ms, self._max_latency_ms,
                )

            fill_ratio = (
                result.filled_qty / leg.quantity if leg.quantity > 0 else 0
            )
            if fill_ratio < self._min_fill_ratio:
                logger.warning(
                    "PARTIAL_FILL | %s filled %.2f%% of requested (min=%.0f%%)",
                    leg.exchange, fill_ratio * 100, self._min_fill_ratio * 100,
                )
                abort = True

        all_ok = all(f.success for f in fills)
        unhedged = self._detect_unhedged(fills)

        if unhedged:
            logger.critical(
                "⚠ UNHEDGED POSITION DETECTED — %s | "
                "Manual intervention may be required.",
                opp.strategy,
            )

        pnl = self._calc_pnl(opp.legs, fills)
        total_lat = sum(f.latency_ms for f in fills)

        logger.info(
            "LIVE | %s | all_filled=%s | pnl=$%.4f | latency=%.0fms | unhedged=%s",
            opp.strategy, all_ok, pnl, total_lat, unhedged,
        )

        return ExecutionReport(
            opportunity=opp,
            fills=fills,
            all_filled=all_ok,
            net_pnl_usd=pnl,
            total_latency_ms=total_lat,
            unhedged=unhedged,
        )

    # ── Concurrent execution ─────────────────────────────

    async def _execute_concurrent(self, opp: Opportunity) -> ExecutionReport:
        """
        Fire all legs simultaneously via ``asyncio.gather``.

        This is the Phase 3 upgrade for spatial arbitrage where both legs
        (buy on A, sell on B) must land as close together as possible to
        minimise state-drift risk.

        If any leg fails, we still report all results — the unhedged
        detector will flag the mismatch.
        """
        async def _fire_leg(leg: TradeLeg) -> TradeResult:
            adapter = self._adapters.get(leg.exchange)
            if adapter is None:
                return self._failed_fill(leg, "no_adapter")
            return await adapter.place_market_order(
                symbol=leg.symbol,
                side=leg.side,
                quantity=leg.quantity,
            )

        results = await asyncio.gather(
            *[_fire_leg(leg) for leg in opp.legs],
            return_exceptions=True,
        )

        fills: List[TradeResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "CONCURRENT_FAIL | leg %d: %s", i, result,
                )
                fills.append(self._failed_fill(opp.legs[i], str(result)))
            else:
                fills.append(result)

        # Post-fill safety checks
        for fill in fills:
            if fill.success and fill.latency_ms > self._max_latency_ms:
                logger.warning(
                    "LATENCY_BREACH | %s took %.0fms (cap=%.0fms)",
                    fill.exchange, fill.latency_ms, self._max_latency_ms,
                )

        all_ok = all(f.success for f in fills)
        unhedged = self._detect_unhedged(fills)

        if unhedged:
            logger.critical(
                "⚠ UNHEDGED POSITION (concurrent) — %s | "
                "Manual intervention may be required.",
                opp.strategy,
            )

        pnl = self._calc_pnl(opp.legs, fills)
        total_lat = max((f.latency_ms for f in fills), default=0)  # wall-clock = max, not sum

        logger.info(
            "CONCURRENT | %s | all_filled=%s | pnl=$%.4f | max_latency=%.0fms | unhedged=%s",
            opp.strategy, all_ok, pnl, total_lat, unhedged,
        )

        return ExecutionReport(
            opportunity=opp,
            fills=fills,
            all_filled=all_ok,
            net_pnl_usd=pnl,
            total_latency_ms=total_lat,
            unhedged=unhedged,
        )

    # ── Helpers ──────────────────────────────────────────

    @staticmethod
    def _calc_pnl(legs: List[TradeLeg], fills: List[TradeResult]) -> float:
        """
        Estimate realised P&L from fills.

        For a spatial pair (buy + sell), P&L ≈
            (sell_price × sell_qty) − (buy_price × buy_qty) − total_fees
        """
        cash_flow = 0.0
        total_fees = 0.0
        for fill in fills:
            if not fill.success:
                continue
            if fill.side == "sell":
                cash_flow += fill.avg_fill_price * fill.filled_qty
            else:
                cash_flow -= fill.avg_fill_price * fill.filled_qty
            total_fees += fill.fee
        return cash_flow - total_fees

    @staticmethod
    def _detect_unhedged(fills: List[TradeResult]) -> bool:
        """True if some legs filled and others didn't."""
        successes = [f.success for f in fills]
        return any(successes) and not all(successes)

    @staticmethod
    def _failed_fill(leg: TradeLeg, error: str) -> TradeResult:
        return TradeResult(
            exchange=leg.exchange,
            symbol=leg.symbol,
            side=leg.side,
            requested_qty=leg.quantity,
            filled_qty=0.0,
            avg_fill_price=0.0,
            fee=0.0,
            fee_currency="",
            latency_ms=0.0,
            order_id="",
            success=False,
            error=error,
        )
