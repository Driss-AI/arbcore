"""
Math & Calculation Utilities
============================
Pure, stateless functions for spread computation, slippage estimation,
and the Bellman-Ford negative-cycle detection used by triangular arb.

Every function in this module is deterministic:
    same inputs → same outputs, no I/O, no side effects.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple


def net_spread_pct(
    bid_price: float,
    ask_price: float,
    fee_buy: float,
    fee_sell: float,
) -> float:
    """
    Compute the net arbitrage spread between two venues after fees.

    Parameters
    ----------
    bid_price : best bid on the *selling* exchange
    ask_price : best ask on the *buying* exchange
    fee_buy   : taker fee on the buying exchange (decimal, e.g. 0.001)
    fee_sell  : taker fee on the selling exchange

    Returns
    -------
    Net spread as a percentage.  Positive = profitable.
    """
    if ask_price <= 0:
        return -math.inf
    effective_buy = ask_price * (1 + fee_buy)
    effective_sell = bid_price * (1 - fee_sell)
    return ((effective_sell - effective_buy) / effective_buy) * 100


def estimate_slippage(
    order_qty: float,
    book_levels: List[Tuple[float, float]],
) -> Tuple[float, float]:
    """
    Walk the order book to estimate fill price and slippage.

    Parameters
    ----------
    order_qty   : quantity to fill
    book_levels : list of (price, quantity) tuples, best-first

    Returns
    -------
    (weighted_avg_price, slippage_pct_from_top)
    """
    if not book_levels:
        return 0.0, math.inf

    filled = 0.0
    cost = 0.0
    top_price = book_levels[0][0]

    for price, qty in book_levels:
        take = min(qty, order_qty - filled)
        cost += take * price
        filled += take
        if filled >= order_qty:
            break

    if filled < order_qty:
        # Insufficient liquidity — signal infinite slippage
        return 0.0, math.inf

    avg_price = cost / filled
    slippage = abs(avg_price - top_price) / top_price * 100
    return avg_price, slippage


def bellman_ford_negative_cycle(
    vertices: List[str],
    edges: List[Tuple[str, str, float]],
) -> Optional[List[str]]:
    """
    Detect a negative-weight cycle in a currency graph.

    Used for triangular arbitrage: edge weights are ``-ln(rate)``.
    A negative cycle means the product of exchange rates along that
    loop exceeds 1.0 → guaranteed arbitrage.

    Parameters
    ----------
    vertices : list of currency symbols
    edges    : list of (from, to, weight) where weight = -ln(exchange_rate)

    Returns
    -------
    The cycle as a list of vertices, or None if no opportunity exists.
    """
    INF = float("inf")
    dist: Dict[str, float] = {v: INF for v in vertices}
    pred: Dict[str, Optional[str]] = {v: None for v in vertices}

    if not vertices:
        return None
    dist[vertices[0]] = 0.0

    # Relax edges |V| - 1 times
    for _ in range(len(vertices) - 1):
        for u, v, w in edges:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                pred[v] = u

    # Check for negative cycle on the |V|-th pass
    cycle_node: Optional[str] = None
    for u, v, w in edges:
        if dist[u] + w < dist[v]:
            cycle_node = v
            break

    if cycle_node is None:
        return None

    # Trace back to extract the cycle
    visited = cycle_node
    for _ in range(len(vertices)):
        visited = pred[visited]  # type: ignore
    cycle = []
    node = visited
    while True:
        cycle.append(node)
        node = pred[node]  # type: ignore
        if node == visited:
            cycle.append(node)
            break
    cycle.reverse()
    return cycle
