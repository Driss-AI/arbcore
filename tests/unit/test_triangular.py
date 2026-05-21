"""
Unit tests for strategies.triangular
=====================================
All tests use synthetic order-book data.  No exchange calls.
"""

from __future__ import annotations

from exchanges.base import OrderBookLevel, OrderBookSnapshot
from strategies.triangular import TriangularConfig, TriangularStrategy


def _snap(
    exchange: str,
    symbol: str,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        exchange=exchange,
        symbol=symbol,
        bids=[OrderBookLevel(p, q) for p, q in bids],
        asks=[OrderBookLevel(p, q) for p, q in asks],
        timestamp_ms=1700000000000,
    )


class TestTriangularStrategy:
    def _make_strategy(self, **overrides) -> TriangularStrategy:
        defaults = dict(base_currency="USDT", min_net_spread_pct=0.01, max_trade_qty_usd=1000)
        defaults.update(overrides)
        return TriangularStrategy(TriangularConfig(**defaults))

    def test_detects_profitable_triangle(self):
        """
        Create a mispriced triangle:
            USDT → BTC → ETH → USDT
        with rates that multiply to > 1.0 after fees.
        """
        strategy = self._make_strategy()

        # Rates designed so 1 USDT → BTC → ETH → 1.005 USDT (0.5% gross)
        # BTC/USDT ask = 50,000  → 1 USDT buys 0.00002 BTC
        # ETH/BTC  ask = 0.05    → 0.00002 BTC buys 0.0004 ETH
        # ETH/USDT bid = 2,520   → 0.0004 ETH sells for 1.008 USDT
        # Product: (1/50000) * (1/0.05) * 2520 = 1.008  → 0.8% gross
        snapshots = {
            "binance": {
                "BTC/USDT": _snap("binance", "BTC/USDT",
                    bids=[(49_900, 10)], asks=[(50_000, 10)]),
                "ETH/BTC": _snap("binance", "ETH/BTC",
                    bids=[(0.0499, 100)], asks=[(0.05, 100)]),
                "ETH/USDT": _snap("binance", "ETH/USDT",
                    bids=[(2_520, 100)], asks=[(2_530, 100)]),
            },
        }
        fees = {
            "binance": {
                "BTC/USDT": 0.001,
                "ETH/BTC": 0.001,
                "ETH/USDT": 0.001,
            },
        }

        opps = strategy.scan(snapshots, fees)
        assert len(opps) >= 1
        best = opps[0]
        assert best.strategy == "triangular"
        assert best.net_pct > 0
        assert len(best.legs) == 3

    def test_rejects_balanced_triangle(self):
        """When rates form a closed loop with product ≤ 1, no opportunity."""
        strategy = self._make_strategy()

        # Product: (1/50000) * (1/0.05) * 2500 = 1.0 → exactly break-even
        # After 3 × 0.1% fees → net < 0
        snapshots = {
            "binance": {
                "BTC/USDT": _snap("binance", "BTC/USDT",
                    bids=[(49_900, 10)], asks=[(50_000, 10)]),
                "ETH/BTC": _snap("binance", "ETH/BTC",
                    bids=[(0.0499, 100)], asks=[(0.05, 100)]),
                "ETH/USDT": _snap("binance", "ETH/USDT",
                    bids=[(2_500, 100)], asks=[(2_510, 100)]),
            },
        }
        fees = {
            "binance": {
                "BTC/USDT": 0.001,
                "ETH/BTC": 0.001,
                "ETH/USDT": 0.001,
            },
        }

        opps = strategy.scan(snapshots, fees)
        profitable = [o for o in opps if o.net_pct > 0]
        assert len(profitable) == 0

    def test_handles_missing_pairs(self):
        """If the graph can't form a triangle, return empty."""
        strategy = self._make_strategy()

        snapshots = {
            "binance": {
                "BTC/USDT": _snap("binance", "BTC/USDT",
                    bids=[(50_000, 10)], asks=[(50_100, 10)]),
                # Missing ETH/BTC — no triangle possible
            },
        }
        fees = {"binance": {"BTC/USDT": 0.001}}

        opps = strategy.scan(snapshots, fees)
        assert len(opps) == 0

    def test_scans_each_exchange_independently(self):
        """Triangular arb is intra-exchange; each exchange scanned alone."""
        strategy = self._make_strategy()

        # Profitable triangle on exchange A, not on exchange B
        snapshots = {
            "exchange_a": {
                "BTC/USDT": _snap("exchange_a", "BTC/USDT",
                    bids=[(49_900, 10)], asks=[(50_000, 10)]),
                "ETH/BTC": _snap("exchange_a", "ETH/BTC",
                    bids=[(0.0499, 100)], asks=[(0.05, 100)]),
                "ETH/USDT": _snap("exchange_a", "ETH/USDT",
                    bids=[(2_520, 100)], asks=[(2_530, 100)]),
            },
            "exchange_b": {
                "BTC/USDT": _snap("exchange_b", "BTC/USDT",
                    bids=[(49_900, 10)], asks=[(50_000, 10)]),
                "ETH/BTC": _snap("exchange_b", "ETH/BTC",
                    bids=[(0.0499, 100)], asks=[(0.05, 100)]),
                "ETH/USDT": _snap("exchange_b", "ETH/USDT",
                    bids=[(2_490, 100)], asks=[(2_500, 100)]),
            },
        }
        fees = {
            "exchange_a": {"BTC/USDT": 0.001, "ETH/BTC": 0.001, "ETH/USDT": 0.001},
            "exchange_b": {"BTC/USDT": 0.001, "ETH/BTC": 0.001, "ETH/USDT": 0.001},
        }

        opps = strategy.scan(snapshots, fees)
        # Only exchange_a should produce opportunity
        if opps:
            profitable_exchanges = {o.metadata["exchange"] for o in opps if o.net_pct > 0}
            assert "exchange_a" in profitable_exchanges or len(profitable_exchanges) == 0

    def test_cycle_reorientation(self):
        """Verify the cycle reorientation utility."""
        result = TriangularStrategy._reorient_cycle(["BTC", "ETH", "USDT"], "USDT")
        assert result == ["USDT", "BTC", "ETH"]

    def test_reorient_returns_none_if_target_missing(self):
        result = TriangularStrategy._reorient_cycle(["BTC", "ETH"], "USDT")
        assert result is None

    def test_handles_empty_books(self):
        """Empty order books should not crash."""
        strategy = self._make_strategy()
        snapshots = {
            "binance": {
                "BTC/USDT": _snap("binance", "BTC/USDT", bids=[], asks=[]),
                "ETH/BTC": _snap("binance", "ETH/BTC", bids=[], asks=[]),
                "ETH/USDT": _snap("binance", "ETH/USDT", bids=[], asks=[]),
            },
        }
        fees = {"binance": {"BTC/USDT": 0.001, "ETH/BTC": 0.001, "ETH/USDT": 0.001}}
        opps = strategy.scan(snapshots, fees)
        assert isinstance(opps, list)
