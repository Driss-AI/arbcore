"""
Spatial (Cross-Exchange) Arbitrage Strategy
===========================================
Implements the delta-neutral inventory model from the research report:

    For each symbol monitored across ≥2 exchanges, detect when
    Exchange A's best bid exceeds Exchange B's best ask by more
    than the combined taker fees + estimated slippage.

The scanner is pure math — no I/O.  It receives pre-fetched order-book
snapshots and fee schedules and returns ``Opportunity`` objects.

Key decisions reflected in the code:
    • Slippage is estimated by walking the book, not assumed constant.
    • Trade size is clamped to the *minimum* available depth across
      both legs so we never project a fill we can't actually get.
    • A minimum net spread threshold (configurable) prevents chasing
      dust-level profits that evaporate under real execution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List

from exchanges.base import OrderBookLevel, OrderBookSnapshot
from strategies.base import BaseStrategy, Opportunity, TradeLeg
from utils.math_helpers import estimate_slippage, net_spread_pct
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SpatialConfig:
    """Tunables for the spatial strategy."""

    symbols: List[str]                # e.g. ["BTC/USDT", "ETH/USDT"]
    min_net_spread_pct: float = 0.05  # ignore anything below 5 bps net
    max_trade_qty_usd: float = 100.0  # per-leg notional cap
    book_depth: int = 5               # levels fetched per snapshot


class SpatialStrategy(BaseStrategy):
    """
    Cross-exchange spatial arbitrage scanner.

    For every (symbol, exchange_A, exchange_B) combination where A ≠ B,
    check both directions (buy on A / sell on B, and vice versa).
    """

    def __init__(self, config: SpatialConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "spatial"

    def scan(
        self,
        snapshots: Dict[str, Dict[str, OrderBookSnapshot]],
        fee_schedule: Dict[str, Dict[str, float]],
    ) -> List[Opportunity]:
        """
        Scan all symbol × exchange-pair combinations for spread > threshold.

        Parameters
        ----------
        snapshots    : {exchange: {symbol: OrderBookSnapshot}}
        fee_schedule : {exchange: {symbol: taker_fee_decimal}}
        """
        opportunities: List[Opportunity] = []

        for symbol in self._config.symbols:
            # Collect exchanges that have data for this symbol
            available = [
                ex for ex in snapshots
                if symbol in snapshots[ex]
                and snapshots[ex][symbol].bids
                and snapshots[ex][symbol].asks
            ]
            if len(available) < 2:
                continue

            for ex_buy, ex_sell in combinations(available, 2):
                # Try direction: buy on ex_buy, sell on ex_sell
                opp = self._evaluate_pair(
                    symbol, ex_buy, ex_sell, snapshots, fee_schedule,
                )
                if opp:
                    opportunities.append(opp)

                # Try reverse direction
                opp_rev = self._evaluate_pair(
                    symbol, ex_sell, ex_buy, snapshots, fee_schedule,
                )
                if opp_rev:
                    opportunities.append(opp_rev)

        # Sort by net profitability descending — engine takes the best one
        opportunities.sort(key=lambda o: o.net_pct, reverse=True)
        return opportunities

    # ── Private ──────────────────────────────────────────

    def _evaluate_pair(
        self,
        symbol: str,
        buy_exchange: str,
        sell_exchange: str,
        snapshots: Dict[str, Dict[str, OrderBookSnapshot]],
        fee_schedule: Dict[str, Dict[str, float]],
    ) -> Opportunity | None:
        """
        Evaluate a single directional pair:
            buy ``symbol`` on ``buy_exchange``, sell on ``sell_exchange``.
        """
        buy_book = snapshots[buy_exchange][symbol]
        sell_book = snapshots[sell_exchange][symbol]

        best_ask = buy_book.asks[0].price   # cheapest place to buy
        best_bid = sell_book.bids[0].price   # best place to sell

        fee_buy = fee_schedule.get(buy_exchange, {}).get(symbol, 0.001)
        fee_sell = fee_schedule.get(sell_exchange, {}).get(symbol, 0.001)

        # ── Quick gross check ────────────────────────────
        gross = ((best_bid - best_ask) / best_ask) * 100 if best_ask > 0 else -math.inf
        if gross <= 0:
            return None  # not even a raw spread — skip immediately

        # ── Size the trade ───────────────────────────────
        # Determine max quantity we can move through the book on both sides
        ask_levels = [(l.price, l.quantity) for l in buy_book.asks]
        bid_levels = [(l.price, l.quantity) for l in sell_book.bids]

        max_qty_buy = sum(q for _, q in ask_levels)
        max_qty_sell = sum(q for _, q in bid_levels)

        # Cap by config notional limit (converted to asset qty via mid price)
        mid_price = (best_ask + best_bid) / 2
        if mid_price <= 0:
            return None
        max_qty_usd = self._config.max_trade_qty_usd / mid_price

        trade_qty = min(max_qty_buy, max_qty_sell, max_qty_usd)
        if trade_qty <= 0:
            return None

        # ── Slippage-aware fill estimation ───────────────
        avg_buy_price, buy_slippage = estimate_slippage(trade_qty, ask_levels)
        avg_sell_price, sell_slippage = estimate_slippage(trade_qty, bid_levels)

        if math.isinf(buy_slippage) or math.isinf(sell_slippage):
            return None  # insufficient depth

        # ── Net spread after fees + slippage ─────────────
        net = net_spread_pct(avg_sell_price, avg_buy_price, fee_buy, fee_sell)

        if net < self._config.min_net_spread_pct:
            return None

        # ── Build the opportunity ────────────────────────
        buy_leg = TradeLeg(
            exchange=buy_exchange,
            symbol=symbol,
            side="buy",
            price=avg_buy_price,
            quantity=trade_qty,
            fee_rate=fee_buy,
        )
        sell_leg = TradeLeg(
            exchange=sell_exchange,
            symbol=symbol,
            side="sell",
            price=avg_sell_price,
            quantity=trade_qty,
            fee_rate=fee_sell,
        )

        logger.info(
            "SPATIAL | %s | buy@%s ask=%.2f → sell@%s bid=%.2f | "
            "qty=%.6f | gross=%.4f%% net=%.4f%% | slip_buy=%.4f%% slip_sell=%.4f%%",
            symbol, buy_exchange, avg_buy_price, sell_exchange, avg_sell_price,
            trade_qty, gross, net, buy_slippage, sell_slippage,
        )

        return Opportunity(
            strategy="spatial",
            legs=[buy_leg, sell_leg],
            gross_pct=gross,
            net_pct=net,
            confidence=1.0,
            metadata={
                "buy_slippage_pct": buy_slippage,
                "sell_slippage_pct": sell_slippage,
                "mid_price": mid_price,
            },
        )
