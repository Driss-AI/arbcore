"""
Base Exchange Adapter
=====================
Defines the abstract contract that every exchange integration must fulfil.
Concrete implementations (e.g. `binance.py`, `kraken.py`) live alongside
this file and are registered with the factory in `__init__.py`.

All exchange I/O is async and returns normalised data structures so that
upstream code (strategies, risk manager) never touches raw exchange payloads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional


# ── Normalised data models ────────────────────────────────

@dataclass(frozen=True)
class OrderBookLevel:
    """Single price/quantity level on the book."""
    price: float
    quantity: float


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Top-of-book snapshot for one symbol on one exchange."""
    exchange: str
    symbol: str          # normalised, e.g. "BTC/USDT"
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    timestamp_ms: int    # exchange-reported or local UTC epoch millis


@dataclass(frozen=True)
class TradeResult:
    """Outcome of an executed order."""
    exchange: str
    symbol: str
    side: str            # "buy" | "sell"
    requested_qty: float
    filled_qty: float
    avg_fill_price: float
    fee: float
    fee_currency: str
    latency_ms: float
    order_id: str
    success: bool
    error: Optional[str] = None


# ── Abstract adapter ──────────────────────────────────────

class BaseExchangeAdapter(ABC):
    """
    Contract for exchange integrations.

    Every concrete adapter wraps a ``ccxt.pro`` async exchange instance
    and normalises its output into the dataclasses above.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Lowercase exchange identifier (e.g. 'binance')."""
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Authenticate and open WebSocket / REST sessions."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Gracefully close all connections."""
        ...

    @abstractmethod
    async def fetch_order_book(
        self, symbol: str, depth: int = 5
    ) -> OrderBookSnapshot:
        """Return a normalised top-of-book snapshot."""
        ...

    @abstractmethod
    async def fetch_balances(self) -> Dict[str, float]:
        """Return available (free) balances keyed by currency."""
        ...

    @abstractmethod
    async def place_market_order(
        self, symbol: str, side: str, quantity: float
    ) -> TradeResult:
        """Execute a market order and return the fill report."""
        ...

    @abstractmethod
    async def fetch_trading_fee(self, symbol: str) -> float:
        """Return the taker fee rate as a decimal (e.g. 0.001 for 0.1%)."""
        ...
