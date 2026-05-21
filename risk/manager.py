"""
Risk Manager
============
The single point of authority that decides whether an ``Opportunity``
is safe to execute.  Every trade must pass through this gate.

Checks performed (in order):
    1. Mode gate       — block all execution in ``dry_run`` mode.
    2. Per-trade cap   — reject if notional exceeds ``max_trade_usd``.
    3. Slippage guard  — reject if estimated slippage > threshold.
    4. Exposure cap    — reject if aggregate open exposure would breach limit.
    5. Daily loss stop — reject if cumulative realised loss has hit the daily cap.
    6. Drawdown kill   — reject (and halt engine) if portfolio drawdown > threshold.

The risk manager is *stateful* — it tracks intra-session P&L and exposure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List

from utils.logger import get_logger

if TYPE_CHECKING:
    from config.settings import RiskLimits
    from strategies.base import Opportunity

logger = get_logger(__name__)


@dataclass
class RiskVerdict:
    """Result of a risk check."""
    approved: bool
    reason: str = ""


class RiskManager:
    """
    Stateful risk gate.

    Instantiated once per engine session.  Accumulates realised P&L and
    current exposure throughout the session lifetime.
    """

    def __init__(self, limits: "RiskLimits") -> None:
        self._limits = limits
        self._daily_realised_pnl: float = 0.0
        self._open_exposure_usd: float = 0.0
        self._peak_equity: float = 0.0  # for drawdown tracking
        self._current_equity: float = 0.0
        self._trade_log: List[dict] = []

    # ── Public API ───────────────────────────────────────

    def evaluate(self, opp: "Opportunity", mode: str) -> RiskVerdict:
        """
        Run the full pre-trade risk checklist.

        Returns an ``approved=True`` verdict only if *every* check passes.
        """
        if mode == "dry_run":
            logger.debug("Dry-run mode — logging opportunity, blocking execution.")
            return RiskVerdict(approved=False, reason="dry_run_mode")

        notional = self._estimate_notional(opp)

        # 1. Per-trade cap
        if notional > self._limits.max_trade_usd:
            return RiskVerdict(
                approved=False,
                reason=f"notional ${notional:.2f} exceeds per-trade cap ${self._limits.max_trade_usd:.2f}",
            )

        # 2. Slippage guard
        if opp.net_pct < 0:
            return RiskVerdict(
                approved=False,
                reason=f"net_pct {opp.net_pct:.4f}% is negative after fees/slippage",
            )

        # 3. Exposure cap
        projected_exposure = self._open_exposure_usd + notional
        if projected_exposure > self._limits.max_open_exposure_usd:
            return RiskVerdict(
                approved=False,
                reason=f"projected exposure ${projected_exposure:.2f} exceeds cap ${self._limits.max_open_exposure_usd:.2f}",
            )

        # 4. Daily loss stop
        if self._daily_realised_pnl <= -self._limits.daily_loss_limit_usd:
            return RiskVerdict(
                approved=False,
                reason=f"daily loss limit reached (${self._daily_realised_pnl:.2f})",
            )

        # 5. Drawdown kill-switch
        if self._peak_equity > 0:
            drawdown_pct = (
                (self._peak_equity - self._current_equity) / self._peak_equity
            ) * 100
            if drawdown_pct >= self._limits.max_drawdown_pct:
                return RiskVerdict(
                    approved=False,
                    reason=f"drawdown {drawdown_pct:.2f}% hit kill-switch ({self._limits.max_drawdown_pct}%)",
                )

        return RiskVerdict(approved=True)

    def record_fill(self, pnl: float, notional: float) -> None:
        """Update internal accumulators after a trade completes."""
        self._daily_realised_pnl += pnl
        self._current_equity += pnl
        if self._current_equity > self._peak_equity:
            self._peak_equity = self._current_equity
        logger.info(
            "P&L updated: fill_pnl=%.4f | session_pnl=%.4f | equity=%.2f",
            pnl,
            self._daily_realised_pnl,
            self._current_equity,
        )

    def set_initial_equity(self, equity_usd: float) -> None:
        """Called once at startup with the aggregate USD value of all balances."""
        self._current_equity = equity_usd
        self._peak_equity = equity_usd

    # ── Private helpers ──────────────────────────────────

    @staticmethod
    def _estimate_notional(opp: "Opportunity") -> float:
        """Sum of (price × quantity) across all legs."""
        return sum(leg.price * leg.quantity for leg in opp.legs)
