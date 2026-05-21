"""Unit tests for utils.math_helpers — deterministic, no I/O."""

import math

from utils.math_helpers import (
    bellman_ford_negative_cycle,
    estimate_slippage,
    net_spread_pct,
)


class TestNetSpreadPct:
    def test_profitable_spread(self):
        # bid=101 on exchange A, ask=100 on exchange B, 0.1% fee each
        result = net_spread_pct(101.0, 100.0, 0.001, 0.001)
        assert result > 0  # should be ~0.8%

    def test_unprofitable_when_fees_exceed_spread(self):
        # bid=100.05, ask=100.0, 0.1% fee each → fees eat the spread
        result = net_spread_pct(100.05, 100.0, 0.001, 0.001)
        assert result < 0

    def test_zero_ask_returns_neg_inf(self):
        result = net_spread_pct(100.0, 0.0, 0.001, 0.001)
        assert result == -math.inf


class TestEstimateSlippage:
    def test_single_level_no_slippage(self):
        book = [(50000.0, 10.0)]
        avg, slip = estimate_slippage(1.0, book)
        assert avg == 50000.0
        assert slip == 0.0

    def test_multi_level_slippage(self):
        book = [(100.0, 1.0), (101.0, 1.0), (102.0, 1.0)]
        avg, slip = estimate_slippage(2.0, book)
        assert avg == 100.5  # (100*1 + 101*1) / 2
        assert slip > 0

    def test_insufficient_liquidity(self):
        book = [(100.0, 0.5)]
        avg, slip = estimate_slippage(5.0, book)
        assert slip == math.inf


class TestBellmanFordNegativeCycle:
    def test_detects_triangular_opportunity(self):
        # USD → EUR → GBP → USD with rates that produce a profit
        vertices = ["USD", "EUR", "GBP"]
        # Rates: USD→EUR 0.82, EUR→GBP 1.19, GBP→USD 1.465
        # Product: 0.82 * 1.19 * 1.465 ≈ 1.429 > 1 → arb exists
        edges = [
            ("USD", "EUR", -math.log(0.82)),
            ("EUR", "GBP", -math.log(1.19)),
            ("GBP", "USD", -math.log(1.465)),
            # Reverse edges (less favorable)
            ("EUR", "USD", -math.log(1.0 / 0.82)),
            ("GBP", "EUR", -math.log(1.0 / 1.19)),
            ("USD", "GBP", -math.log(1.0 / 1.465)),
        ]
        cycle = bellman_ford_negative_cycle(vertices, edges)
        assert cycle is not None
        assert len(cycle) >= 3

    def test_no_opportunity_when_rates_unfavorable(self):
        vertices = ["USD", "EUR", "GBP"]
        # Rates that multiply to < 1.0 → no arb (fees eat the spread)
        # USD→EUR 0.80, EUR→GBP 1.10, GBP→USD 1.10
        # Product: 0.80 * 1.10 * 1.10 = 0.968 < 1 → loss
        edges = [
            ("USD", "EUR", -math.log(0.80)),
            ("EUR", "GBP", -math.log(1.10)),
            ("GBP", "USD", -math.log(1.10)),
        ]
        cycle = bellman_ford_negative_cycle(vertices, edges)
        assert cycle is None
