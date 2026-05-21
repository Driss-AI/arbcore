"""
Tests for Phase 3: health monitor, concurrent executor, settings updates.
"""

from __future__ import annotations

import pytest

from core.executor import Executor, ExecutionReport
from core.health import HealthMonitor
from strategies.base import Opportunity, TradeLeg


# ── Health Monitor ───────────────────────────────────────

class TestHealthMonitor:
    def test_record_tick_increments_stats(self):
        h = HealthMonitor(port=0)  # port 0 = don't actually bind
        h.record_tick(opportunities=3, trades=1, pnl_delta=5.50)
        h.record_tick(opportunities=1, trades=0, pnl_delta=-0.25)

        assert h._stats["tick_count"] == 2
        assert h._stats["opportunities_found"] == 4
        assert h._stats["trades_executed"] == 1
        assert abs(h._stats["session_pnl_usd"] - 5.25) < 0.01

    def test_update_stats_merges(self):
        h = HealthMonitor()
        h.update_stats(mode="dry_run", exchanges=["binance"])
        assert h._stats["mode"] == "dry_run"
        assert h._stats["exchanges"] == ["binance"]

    def test_initial_stats_have_defaults(self):
        h = HealthMonitor()
        assert h._stats["tick_count"] == 0
        assert h._stats["session_pnl_usd"] == 0.0


# ── Concurrent Executor ──────────────────────────────────

def _make_opp(
    buy_price: float = 50_000,
    sell_price: float = 50_100,
    qty: float = 0.1,
    fee: float = 0.001,
) -> Opportunity:
    return Opportunity(
        strategy="spatial",
        legs=[
            TradeLeg("exA", "BTC/USDT", "buy", buy_price, qty, fee),
            TradeLeg("exB", "BTC/USDT", "sell", sell_price, qty, fee),
        ],
        gross_pct=0.2,
        net_pct=0.1,
    )


class TestConcurrentExecutor:
    def test_dry_run_unaffected_by_concurrent_flag(self):
        """Concurrent flag should not change dry_run behavior."""
        ex_seq = Executor(adapters={}, mode="dry_run", concurrent=False)
        ex_conc = Executor(adapters={}, mode="dry_run", concurrent=True)

        opp = _make_opp()
        report_seq = ex_seq._simulate(opp)
        report_conc = ex_conc._simulate(opp)

        assert report_seq.all_filled == report_conc.all_filled
        assert abs(report_seq.net_pnl_usd - report_conc.net_pnl_usd) < 0.01

    def test_concurrent_flag_stored(self):
        ex = Executor(adapters={}, mode="live", concurrent=True)
        assert ex._concurrent is True

    def test_sequential_executor_has_concurrent_false(self):
        ex = Executor(adapters={}, mode="live", concurrent=False)
        assert ex._concurrent is False


# ── Settings Phase 3 Fields ──────────────────────────────

class TestSettingsPhase3:
    def test_default_strategy_is_both(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        from config.settings import Settings
        s = Settings.load(env_path=env_file)
        assert s.strategy == "both"

    def test_websocket_default_off(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        from config.settings import Settings
        s = Settings.load(env_path=env_file)
        assert s.use_websocket is False

    def test_concurrent_default_off(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        from config.settings import Settings
        s = Settings.load(env_path=env_file)
        assert s.concurrent_execution is False

    def test_strategy_env_var(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        monkeypatch.setenv("ARBCORE_STRATEGY", "triangular")
        from config.settings import Settings
        s = Settings.load(env_path=env_file)
        assert s.strategy == "triangular"

    def test_invalid_strategy_raises(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        monkeypatch.setenv("ARBCORE_STRATEGY", "moonshot")
        from config.settings import Settings
        with pytest.raises(ValueError, match="spatial"):
            Settings.load(env_path=env_file)

    def test_websocket_enabled(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        monkeypatch.setenv("ARBCORE_USE_WEBSOCKET", "true")
        from config.settings import Settings
        s = Settings.load(env_path=env_file)
        assert s.use_websocket is True

    def test_health_port_from_env(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        monkeypatch.setenv("ARBCORE_HEALTH_PORT", "9090")
        from config.settings import Settings
        s = Settings.load(env_path=env_file)
        assert s.health_port == 9090
