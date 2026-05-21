"""
Unit tests for risk.manager
============================
Validates every gate in the risk checklist against real Opportunity objects.
"""

from __future__ import annotations

from config.settings import RiskLimits
from risk.manager import RiskManager
from strategies.base import Opportunity, TradeLeg


def _make_opp(
    notional_per_leg: float = 50.0,
    net_pct: float = 0.10,
) -> Opportunity:
    """Build an opportunity with total notional = 2 × notional_per_leg."""
    price = notional_per_leg / 0.1  # qty = 0.1
    return Opportunity(
        strategy="spatial",
        legs=[
            TradeLeg("exA", "BTC/USDT", "buy", price, 0.1, 0.001),
            TradeLeg("exB", "BTC/USDT", "sell", price + 10, 0.1, 0.001),
        ],
        gross_pct=0.20,
        net_pct=net_pct,
    )


class TestRiskManagerGates:

    def test_blocks_in_dry_run(self):
        rm = RiskManager(RiskLimits())
        opp = _make_opp()
        verdict = rm.evaluate(opp, mode="dry_run")
        assert verdict.approved is False
        assert "dry_run" in verdict.reason

    def test_approves_within_limits_live(self):
        rm = RiskManager(RiskLimits(max_trade_usd=200, max_open_exposure_usd=1000))
        rm.set_initial_equity(10_000)
        opp = _make_opp(notional_per_leg=50.0)
        verdict = rm.evaluate(opp, mode="live")
        assert verdict.approved is True

    def test_blocks_over_trade_cap(self):
        rm = RiskManager(RiskLimits(max_trade_usd=10.0))
        rm.set_initial_equity(10_000)
        opp = _make_opp(notional_per_leg=50.0)  # total notional ~100
        verdict = rm.evaluate(opp, mode="live")
        assert verdict.approved is False
        assert "per-trade cap" in verdict.reason

    def test_blocks_negative_net_pct(self):
        rm = RiskManager(RiskLimits(max_trade_usd=10_000))
        rm.set_initial_equity(10_000)
        opp = _make_opp(net_pct=-0.05)
        verdict = rm.evaluate(opp, mode="live")
        assert verdict.approved is False
        assert "negative" in verdict.reason

    def test_blocks_exposure_breach(self):
        rm = RiskManager(RiskLimits(max_trade_usd=10_000, max_open_exposure_usd=50))
        rm.set_initial_equity(10_000)
        opp = _make_opp(notional_per_leg=50.0)  # ~100 total > 50 cap
        verdict = rm.evaluate(opp, mode="live")
        assert verdict.approved is False
        assert "exposure" in verdict.reason

    def test_blocks_after_daily_loss_limit(self):
        rm = RiskManager(RiskLimits(
            max_trade_usd=10_000,
            max_open_exposure_usd=10_000,
            daily_loss_limit_usd=10.0,
        ))
        rm.set_initial_equity(10_000)
        # Simulate a losing day
        rm.record_fill(-15.0, 100.0)
        opp = _make_opp()
        verdict = rm.evaluate(opp, mode="live")
        assert verdict.approved is False
        assert "daily loss" in verdict.reason

    def test_blocks_on_drawdown_kill_switch(self):
        rm = RiskManager(RiskLimits(
            max_trade_usd=10_000,
            max_open_exposure_usd=10_000,
            daily_loss_limit_usd=1_000,
            max_drawdown_pct=1.0,
        ))
        rm.set_initial_equity(10_000)
        # Lose 2% from peak → should trigger 1% kill switch
        rm.record_fill(-200.0, 500.0)
        opp = _make_opp()
        verdict = rm.evaluate(opp, mode="live")
        assert verdict.approved is False
        assert "drawdown" in verdict.reason

    def test_record_fill_updates_equity(self):
        rm = RiskManager(RiskLimits())
        rm.set_initial_equity(1000.0)
        rm.record_fill(5.0, 100.0)
        assert rm._current_equity == 1005.0
        assert rm._peak_equity == 1005.0
        assert rm._daily_realised_pnl == 5.0
