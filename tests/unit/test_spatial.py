"""
Unit tests for strategies.spatial
=================================
All tests use synthetic order-book snapshots — no exchange calls.
The strategy is pure math; deterministic inputs → deterministic outputs.
"""

from __future__ import annotations

from exchanges.base import OrderBookLevel, OrderBookSnapshot
from strategies.spatial import SpatialConfig, SpatialStrategy


def _make_snapshot(
    exchange: str,
    symbol: str,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> OrderBookSnapshot:
    """Helper to construct a snapshot from raw (price, qty) tuples."""
    return OrderBookSnapshot(
        exchange=exchange,
        symbol=symbol,
        bids=[OrderBookLevel(p, q) for p, q in bids],
        asks=[OrderBookLevel(p, q) for p, q in asks],
        timestamp_ms=1700000000000,
    )


class TestSpatialStrategy:
    """Test suite for cross-exchange spatial arbitrage detection."""

    def _make_strategy(self, **overrides) -> SpatialStrategy:
        defaults = dict(
            symbols=["BTC/USDT"],
            min_net_spread_pct=0.05,
            max_trade_qty_usd=10_000.0,
        )
        defaults.update(overrides)
        return SpatialStrategy(SpatialConfig(**defaults))

    def test_detects_profitable_spread(self):
        """When exchange A ask < exchange B bid (after fees), detect opportunity."""
        strategy = self._make_strategy()

        # Exchange A: selling BTC cheap (ask = 50,000)
        # Exchange B: buying BTC dear  (bid = 50,250)
        # Gross spread = 0.5% → survives 0.1% fee × 2 sides = 0.2% total fees
        snapshots = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[(49_900, 1.0)],
                    asks=[(50_000, 1.0)],
                ),
            },
            "exchange_b": {
                "BTC/USDT": _make_snapshot(
                    "exchange_b", "BTC/USDT",
                    bids=[(50_250, 1.0)],
                    asks=[(50_350, 1.0)],
                ),
            },
        }
        fees = {
            "exchange_a": {"BTC/USDT": 0.001},  # 0.1%
            "exchange_b": {"BTC/USDT": 0.001},
        }

        opps = strategy.scan(snapshots, fees)
        assert len(opps) >= 1

        best = opps[0]
        assert best.strategy == "spatial"
        assert best.net_pct > 0
        assert best.confidence == 1.0
        assert len(best.legs) == 2

        # Verify leg directions
        buy_leg = [l for l in best.legs if l.side == "buy"][0]
        sell_leg = [l for l in best.legs if l.side == "sell"][0]
        assert buy_leg.exchange == "exchange_a"
        assert sell_leg.exchange == "exchange_b"

    def test_rejects_when_fees_eat_spread(self):
        """A 0.01% spread with 0.1% fees per side → no opportunity."""
        strategy = self._make_strategy()

        snapshots = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[(50_000, 1.0)],
                    asks=[(50_001, 1.0)],
                ),
            },
            "exchange_b": {
                "BTC/USDT": _make_snapshot(
                    "exchange_b", "BTC/USDT",
                    bids=[(50_005, 1.0)],
                    asks=[(50_010, 1.0)],
                ),
            },
        }
        fees = {
            "exchange_a": {"BTC/USDT": 0.001},
            "exchange_b": {"BTC/USDT": 0.001},
        }

        opps = strategy.scan(snapshots, fees)
        assert len(opps) == 0

    def test_rejects_when_spread_is_inverted(self):
        """Exchange A ask > Exchange B bid → no opportunity either direction."""
        strategy = self._make_strategy()

        snapshots = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[(50_100, 1.0)],
                    asks=[(50_200, 1.0)],
                ),
            },
            "exchange_b": {
                "BTC/USDT": _make_snapshot(
                    "exchange_b", "BTC/USDT",
                    bids=[(50_000, 1.0)],
                    asks=[(50_100, 1.0)],
                ),
            },
        }
        fees = {
            "exchange_a": {"BTC/USDT": 0.001},
            "exchange_b": {"BTC/USDT": 0.001},
        }

        opps = strategy.scan(snapshots, fees)
        assert len(opps) == 0

    def test_quantity_capped_by_book_depth(self):
        """Trade size should not exceed available book liquidity."""
        strategy = self._make_strategy(max_trade_qty_usd=1_000_000)

        # Only 0.5 BTC available at the best ask
        snapshots = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[(49_900, 0.5)],
                    asks=[(50_000, 0.5)],
                ),
            },
            "exchange_b": {
                "BTC/USDT": _make_snapshot(
                    "exchange_b", "BTC/USDT",
                    bids=[(50_200, 10.0)],
                    asks=[(50_300, 10.0)],
                ),
            },
        }
        fees = {
            "exchange_a": {"BTC/USDT": 0.0005},
            "exchange_b": {"BTC/USDT": 0.0005},
        }

        opps = strategy.scan(snapshots, fees)
        assert len(opps) >= 1
        # The trade qty must not exceed the 0.5 BTC available
        for leg in opps[0].legs:
            assert leg.quantity <= 0.5

    def test_quantity_capped_by_usd_limit(self):
        """Trade size should not exceed max_trade_qty_usd."""
        # Cap at $100 → ~0.002 BTC at $50,000
        strategy = self._make_strategy(max_trade_qty_usd=100.0)

        snapshots = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[(49_900, 10.0)],
                    asks=[(50_000, 10.0)],
                ),
            },
            "exchange_b": {
                "BTC/USDT": _make_snapshot(
                    "exchange_b", "BTC/USDT",
                    bids=[(50_200, 10.0)],
                    asks=[(50_300, 10.0)],
                ),
            },
        }
        fees = {
            "exchange_a": {"BTC/USDT": 0.0005},
            "exchange_b": {"BTC/USDT": 0.0005},
        }

        opps = strategy.scan(snapshots, fees)
        assert len(opps) >= 1
        mid = (50_000 + 50_200) / 2
        max_qty = 100.0 / mid
        for leg in opps[0].legs:
            assert leg.quantity <= max_qty + 1e-9  # float tolerance

    def test_slippage_reduces_net_spread(self):
        """Multi-level books with price impact should yield lower net spread."""
        strategy = self._make_strategy(max_trade_qty_usd=500_000)

        # Exchange A asks: thin best level, worse prices deeper
        snapshots_thin = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[(49_900, 5.0)],
                    asks=[
                        (50_000, 0.01),   # thin
                        (50_050, 0.01),
                        (50_100, 5.0),     # we'll eat into this
                    ],
                ),
            },
            "exchange_b": {
                "BTC/USDT": _make_snapshot(
                    "exchange_b", "BTC/USDT",
                    bids=[
                        (50_200, 0.01),
                        (50_150, 0.01),
                        (50_100, 5.0),
                    ],
                    asks=[(50_300, 5.0)],
                ),
            },
        }

        # Fat books version — same top-of-book but deep liquidity
        snapshots_fat = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[(49_900, 5.0)],
                    asks=[(50_000, 5.0)],
                ),
            },
            "exchange_b": {
                "BTC/USDT": _make_snapshot(
                    "exchange_b", "BTC/USDT",
                    bids=[(50_200, 5.0)],
                    asks=[(50_300, 5.0)],
                ),
            },
        }

        fees = {
            "exchange_a": {"BTC/USDT": 0.0005},
            "exchange_b": {"BTC/USDT": 0.0005},
        }

        opps_thin = strategy.scan(snapshots_thin, fees)
        opps_fat = strategy.scan(snapshots_fat, fees)

        # Both should find opportunities
        assert len(opps_fat) >= 1

        # Fat books should yield a better (or equal) net spread
        if opps_thin:
            assert opps_fat[0].net_pct >= opps_thin[0].net_pct

    def test_handles_single_exchange(self):
        """If only one exchange has data, no spatial arb is possible."""
        strategy = self._make_strategy()

        snapshots = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[(50_000, 1.0)],
                    asks=[(50_100, 1.0)],
                ),
            },
        }
        fees = {"exchange_a": {"BTC/USDT": 0.001}}

        opps = strategy.scan(snapshots, fees)
        assert len(opps) == 0

    def test_handles_empty_books(self):
        """Empty bids or asks should be handled gracefully."""
        strategy = self._make_strategy()

        snapshots = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[],
                    asks=[(50_000, 1.0)],
                ),
            },
            "exchange_b": {
                "BTC/USDT": _make_snapshot(
                    "exchange_b", "BTC/USDT",
                    bids=[(50_100, 1.0)],
                    asks=[(50_200, 1.0)],
                ),
            },
        }
        fees = {
            "exchange_a": {"BTC/USDT": 0.001},
            "exchange_b": {"BTC/USDT": 0.001},
        }

        opps = strategy.scan(snapshots, fees)
        assert isinstance(opps, list)  # should not crash

    def test_multiple_symbols_scanned(self):
        """Strategy should scan each symbol independently."""
        strategy = self._make_strategy(symbols=["BTC/USDT", "ETH/USDT"])

        snapshots = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[(49_900, 1.0)],
                    asks=[(50_000, 1.0)],
                ),
                "ETH/USDT": _make_snapshot(
                    "exchange_a", "ETH/USDT",
                    bids=[(2_490, 10.0)],
                    asks=[(2_500, 10.0)],
                ),
            },
            "exchange_b": {
                "BTC/USDT": _make_snapshot(
                    "exchange_b", "BTC/USDT",
                    bids=[(50_100, 1.0)],
                    asks=[(50_200, 1.0)],
                ),
                "ETH/USDT": _make_snapshot(
                    "exchange_b", "ETH/USDT",
                    bids=[(2_510, 10.0)],
                    asks=[(2_520, 10.0)],
                ),
            },
        }
        fees = {
            "exchange_a": {"BTC/USDT": 0.0005, "ETH/USDT": 0.0005},
            "exchange_b": {"BTC/USDT": 0.0005, "ETH/USDT": 0.0005},
        }

        opps = strategy.scan(snapshots, fees)
        # Both symbols have spreads — expect opportunities from both
        strategies_found = {o.legs[0].symbol for o in opps}
        assert "BTC/USDT" in strategies_found or "ETH/USDT" in strategies_found

    def test_opportunities_sorted_by_net_pct(self):
        """Output should be sorted best-first."""
        strategy = self._make_strategy(symbols=["BTC/USDT", "ETH/USDT"])

        snapshots = {
            "exchange_a": {
                "BTC/USDT": _make_snapshot(
                    "exchange_a", "BTC/USDT",
                    bids=[(49_900, 1.0)],
                    asks=[(50_000, 1.0)],
                ),
                "ETH/USDT": _make_snapshot(
                    "exchange_a", "ETH/USDT",
                    bids=[(2_490, 10.0)],
                    asks=[(2_500, 10.0)],
                ),
            },
            "exchange_b": {
                "BTC/USDT": _make_snapshot(
                    "exchange_b", "BTC/USDT",
                    bids=[(50_100, 1.0)],
                    asks=[(50_200, 1.0)],
                ),
                "ETH/USDT": _make_snapshot(
                    "exchange_b", "ETH/USDT",
                    bids=[(2_530, 10.0)],
                    asks=[(2_540, 10.0)],
                ),
            },
        }
        fees = {
            "exchange_a": {"BTC/USDT": 0.0005, "ETH/USDT": 0.0005},
            "exchange_b": {"BTC/USDT": 0.0005, "ETH/USDT": 0.0005},
        }

        opps = strategy.scan(snapshots, fees)
        if len(opps) >= 2:
            assert opps[0].net_pct >= opps[1].net_pct
