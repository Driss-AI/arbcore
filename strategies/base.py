"""
Base Strategy Interface
=======================
All arbitrage strategies (spatial, triangular, stat-arb) inherit from
``BaseStrategy`` and implement a deterministic ``scan()`` method.

The scan method receives market data and returns zero or more
``Opportunity`` objects.  It must be pure math — no I/O, no side effects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from exchanges.base import OrderBookSnapshot


@dataclass(frozen=True)
class TradeLeg:
    """One leg of a multi-leg arbitrage opportunity."""
    exchange: str
    symbol: str
    side: str          # "buy" | "sell"
    price: float       # expected execution price
    quantity: float
    fee_rate: float    # decimal taker fee


@dataclass(frozen=True)
class Opportunity:
    """
    A fully-specified, risk-validated arbitrage opportunity.

    Fields
    ------
    strategy   : name of the strategy that detected it (e.g. "spatial")
    legs       : ordered list of trades to execute
    gross_pct  : gross spread before fees (%)
    net_pct    : spread after fees and estimated slippage (%)
    confidence : 1.0 for deterministic arb, <1.0 for probabilistic
    """
    strategy: str
    legs: List[TradeLeg]
    gross_pct: float
    net_pct: float
    confidence: float = 1.0
    metadata: Optional[dict] = None


class BaseStrategy(ABC):
    """
    Abstract base for all arbitrage detection strategies.

    Implementations must be stateless and deterministic:
    given identical inputs, ``scan()`` must return identical outputs.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name."""
        ...

    @abstractmethod
    def scan(
        self,
        snapshots: dict[str, dict[str, OrderBookSnapshot]],
        fee_schedule: dict[str, dict[str, float]],
    ) -> List[Opportunity]:
        """
        Scan current market state for arbitrage opportunities.

        Parameters
        ----------
        snapshots : nested dict  →  {exchange: {symbol: OrderBookSnapshot}}
        fee_schedule : nested dict → {exchange: {symbol: taker_fee_decimal}}

        Returns
        -------
        List of ``Opportunity`` objects, possibly empty.
        """
        ...
