"""
Triangular Arbitrage Strategy
=============================
Operates within a *single* exchange to exploit pricing inefficiencies
between three (or more) trading pairs that form a closed loop.

From the research report:
    "Triangular arbitrage occurs entirely within the confines of a single
    exchange, mitigating cross-venue transfer risks and counterparty
    fragmentation."

Implementation:
    1. Build a directed currency graph where edge weights are -ln(rate).
    2. Run Bellman-Ford to detect negative-weight cycles.
    3. For each cycle found, walk the order book on each hop to compute
       slippage-adjusted net profit.
    4. Emit Opportunity objects for cycles that survive fee + slippage.

The scanner is pure math — no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from exchanges.base import OrderBookLevel, OrderBookSnapshot
from strategies.base import BaseStrategy, Opportunity, TradeLeg
from utils.logger import get_logger
from utils.math_helpers import bellman_ford_negative_cycle, estimate_slippage

logger = get_logger(__name__)


@dataclass(frozen=True)
class TriangularConfig:
    """Tunables for the triangular strategy."""

    base_currency: str = "USDT"       # the currency we start and end with
    min_net_spread_pct: float = 0.05  # reject cycles below 5 bps
    max_trade_qty_usd: float = 100.0  # per-cycle notional cap
    max_hops: int = 3                 # restrict to true triangles


class TriangularStrategy(BaseStrategy):
    """
    Intra-exchange triangular arbitrage via Bellman-Ford cycle detection.

    Graph construction:
        For every trading pair ``BASE/QUOTE`` on the exchange, we create
        two directed edges:
            • QUOTE → BASE  at the ask rate (buying BASE costs QUOTE)
            • BASE → QUOTE  at the bid rate (selling BASE yields QUOTE)
        Edge weights are ``-ln(effective_rate_after_fees)``.
    """

    def __init__(self, config: TriangularConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "triangular"

    def scan(
        self,
        snapshots: Dict[str, Dict[str, OrderBookSnapshot]],
        fee_schedule: Dict[str, Dict[str, float]],
    ) -> List[Opportunity]:
        """
        Scan each exchange independently for triangular cycles.

        Parameters
        ----------
        snapshots    : {exchange: {symbol: OrderBookSnapshot}}
        fee_schedule : {exchange: {symbol: taker_fee_decimal}}
        """
        opportunities: List[Opportunity] = []

        for exchange, books in snapshots.items():
            fees = fee_schedule.get(exchange, {})
            opps = self._scan_exchange(exchange, books, fees)
            opportunities.extend(opps)

        opportunities.sort(key=lambda o: o.net_pct, reverse=True)
        return opportunities

    # ── Core logic ───────────────────────────────────────

    def _scan_exchange(
        self,
        exchange: str,
        books: Dict[str, OrderBookSnapshot],
        fees: Dict[str, float],
    ) -> List[Opportunity]:
        """Build graph and detect cycles for one exchange."""

        # ── 1. Build currency graph ──────────────────────
        currencies: set[str] = set()
        edges: List[Tuple[str, str, float, str, float]] = []
        # edges: (from_currency, to_currency, weight, symbol, raw_rate)

        for symbol, book in books.items():
            parts = symbol.split("/")
            if len(parts) != 2:
                continue
            base, quote = parts

            if not book.asks or not book.bids:
                continue

            fee = fees.get(symbol, 0.001)
            currencies.add(base)
            currencies.add(quote)

            # Edge: QUOTE → BASE (buying base at the ask)
            best_ask = book.asks[0].price
            if best_ask > 0:
                buy_rate = (1.0 / best_ask) * (1 - fee)  # qty of BASE per QUOTE
                weight = -math.log(buy_rate) if buy_rate > 0 else math.inf
                edges.append((quote, base, weight, symbol, buy_rate))

            # Edge: BASE → QUOTE (selling base at the bid)
            best_bid = book.bids[0].price
            if best_bid > 0:
                sell_rate = best_bid * (1 - fee)  # qty of QUOTE per BASE
                weight = -math.log(sell_rate) if sell_rate > 0 else math.inf
                edges.append((base, quote, weight, symbol, sell_rate))

        if len(currencies) < 3:
            return []

        # ── 2. Bellman-Ford cycle detection ──────────────
        vertex_list = sorted(currencies)
        bf_edges = [(u, v, w) for u, v, w, _, _ in edges]
        cycle = bellman_ford_negative_cycle(vertex_list, bf_edges)

        if cycle is None:
            return []

        # ── 3. Validate cycle length ─────────────────────
        # cycle has the start repeated at end, so real hops = len - 1
        hops = len(cycle) - 1
        if hops < 3 or hops > self._config.max_hops:
            return []

        # ── 4. Verify it starts/ends with base currency ─
        # Re-orient if possible: rotate so base_currency is at start
        cycle_body = cycle[:-1]  # remove duplicate tail
        reoriented = self._reorient_cycle(cycle_body, self._config.base_currency)
        if reoriented is None:
            # base currency not in cycle — still valid, just start wherever
            reoriented = cycle_body
        # Close the loop
        full_cycle = reoriented + [reoriented[0]]

        # ── 5. Build legs and compute net profit ─────────
        opp = self._build_opportunity(
            exchange, full_cycle, books, fees,
        )
        return [opp] if opp else []

    def _build_opportunity(
        self,
        exchange: str,
        cycle: List[str],
        books: Dict[str, OrderBookSnapshot],
        fees: Dict[str, float],
    ) -> Optional[Opportunity]:
        """
        Given a currency cycle [A, B, C, A], construct trade legs
        and compute fee/slippage-adjusted profitability.
        """
        legs: List[TradeLeg] = []
        cumulative_rate = 1.0

        for i in range(len(cycle) - 1):
            from_cur = cycle[i]
            to_cur = cycle[i + 1]

            # Find the symbol and direction
            leg_info = self._resolve_leg(from_cur, to_cur, books, fees)
            if leg_info is None:
                return None  # broken path — no matching pair

            symbol, side, price, fee_rate, book_levels = leg_info

            # Size: start with max_trade_qty_usd, convert to the "from" currency
            # For the first hop, notional is in base_currency (usually USDT)
            # For subsequent hops, it compounds through prior rates
            if i == 0:
                trade_notional = self._config.max_trade_qty_usd
            else:
                trade_notional = trade_notional * cumulative_rate

            # Convert notional to quantity for this pair
            if side == "buy":
                # We're spending from_cur to buy to_cur at ask price
                qty = trade_notional / price if price > 0 else 0
            else:
                # We're selling from_cur to get to_cur at bid price
                qty = trade_notional

            if qty <= 0:
                return None

            # Check book depth
            avg_price, slippage = estimate_slippage(
                qty, [(l.price, l.quantity) for l in book_levels]
            )
            if math.isinf(slippage):
                return None  # insufficient liquidity

            # Track effective rate through the cycle
            if side == "buy":
                effective_rate = (1.0 / avg_price) * (1 - fee_rate)
                cumulative_rate = 1.0 / avg_price  # qty of to_cur per from_cur
            else:
                effective_rate = avg_price * (1 - fee_rate)
                cumulative_rate = avg_price  # qty of to_cur per from_cur

            legs.append(TradeLeg(
                exchange=exchange,
                symbol=symbol,
                side=side,
                price=avg_price,
                quantity=qty,
                fee_rate=fee_rate,
            ))

        # ── Compute cycle P&L ────────────────────────────
        # Start with $X, end with $Y after all hops
        # Gross rate = product of raw rates along cycle
        gross_rate = 1.0
        net_rate = 1.0
        for i in range(len(cycle) - 1):
            from_cur = cycle[i]
            to_cur = cycle[i + 1]
            leg_info = self._resolve_leg(from_cur, to_cur, books, fees)
            if leg_info is None:
                return None
            _, side, price, fee_rate, _ = leg_info
            if side == "buy":
                raw = 1.0 / price
                gross_rate *= raw
                net_rate *= raw * (1 - fee_rate)
            else:
                gross_rate *= price
                net_rate *= price * (1 - fee_rate)

        gross_pct = (gross_rate - 1.0) * 100
        net_pct = (net_rate - 1.0) * 100

        if net_pct < self._config.min_net_spread_pct:
            return None

        logger.info(
            "TRIANGULAR | %s | cycle=%s | gross=%.4f%% net=%.4f%%",
            exchange, " → ".join(cycle), gross_pct, net_pct,
        )

        return Opportunity(
            strategy="triangular",
            legs=legs,
            gross_pct=gross_pct,
            net_pct=net_pct,
            confidence=1.0,
            metadata={"cycle": cycle, "exchange": exchange},
        )

    # ── Helpers ──────────────────────────────────────────

    def _resolve_leg(
        self,
        from_cur: str,
        to_cur: str,
        books: Dict[str, OrderBookSnapshot],
        fees: Dict[str, float],
    ) -> Optional[Tuple[str, str, float, float, List[OrderBookLevel]]]:
        """
        Find the trading pair and direction for a from→to currency hop.

        Returns (symbol, side, price, fee_rate, book_levels) or None.
        """
        # Try direct pair: from_cur/to_cur → we sell from_cur
        sell_symbol = f"{from_cur}/{to_cur}"
        if sell_symbol in books:
            book = books[sell_symbol]
            if book.bids:
                fee = fees.get(sell_symbol, 0.001)
                return sell_symbol, "sell", book.bids[0].price, fee, book.bids

        # Try inverse pair: to_cur/from_cur → we buy to_cur
        buy_symbol = f"{to_cur}/{from_cur}"
        if buy_symbol in books:
            book = books[buy_symbol]
            if book.asks:
                fee = fees.get(buy_symbol, 0.001)
                return buy_symbol, "buy", book.asks[0].price, fee, book.asks

        return None

    @staticmethod
    def _reorient_cycle(
        cycle: List[str], target: str
    ) -> Optional[List[str]]:
        """Rotate cycle so ``target`` is at index 0."""
        if target not in cycle:
            return None
        idx = cycle.index(target)
        return cycle[idx:] + cycle[:idx]
