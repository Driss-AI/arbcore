"""
Generic CCXT Exchange Adapter
=============================
Wraps any exchange supported by ``ccxt`` into the ``BaseExchangeAdapter``
contract.  One class handles Binance, Kraken, OKX, etc. — the exchange
name is passed at construction time and ccxt handles the rest.

All I/O is async.  All return types are the frozen dataclasses from
``exchanges.base``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, List, Optional

import ccxt.async_support as ccxt_async

from exchanges.base import (
    BaseExchangeAdapter,
    OrderBookLevel,
    OrderBookSnapshot,
    TradeResult,
)
from utils.logger import get_logger

if TYPE_CHECKING:
    from config.settings import ExchangeCredentials

logger = get_logger(__name__)

# Exchanges that require a passphrase on top of key/secret
_PASSPHRASE_EXCHANGES = frozenset({"okx", "kucoin", "coinbasepro"})


class CCXTAdapter(BaseExchangeAdapter):
    """
    Concrete adapter backed by ``ccxt.async_support``.

    Parameters
    ----------
    exchange_id : ccxt exchange identifier (e.g. ``"binance"``)
    credentials : API key/secret/passphrase container
    sandbox     : if True, connect to the exchange's testnet
    """

    def __init__(
        self,
        exchange_id: str,
        credentials: "ExchangeCredentials",
        sandbox: bool = False,
    ) -> None:
        self._exchange_id = exchange_id.lower()
        self._credentials = credentials
        self._sandbox = sandbox
        self._client: Optional[ccxt_async.Exchange] = None

        # Cache for trading fees so we don't hit the API every tick
        self._fee_cache: Dict[str, float] = {}

    # ── BaseExchangeAdapter interface ────────────────────

    @property
    def name(self) -> str:
        return self._exchange_id

    async def connect(self) -> None:
        """Instantiate the ccxt async client and load markets."""
        exchange_class = getattr(ccxt_async, self._exchange_id, None)
        if exchange_class is None:
            raise ValueError(
                f"ccxt does not support exchange '{self._exchange_id}'"
            )

        config: dict = {
            "apiKey": self._credentials.api_key or None,
            "secret": self._credentials.api_secret or None,
            "enableRateLimit": True,
            "timeout": 10_000,
        }
        if (
            self._exchange_id in _PASSPHRASE_EXCHANGES
            and self._credentials.passphrase
        ):
            config["password"] = self._credentials.passphrase

        self._client = exchange_class(config)

        if self._sandbox:
            self._client.set_sandbox_mode(True)

        await self._client.load_markets()
        logger.info(
            "%s connected — %d markets loaded (sandbox=%s)",
            self._exchange_id,
            len(self._client.markets),
            self._sandbox,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            logger.info("%s connection closed.", self._exchange_id)

    async def fetch_order_book(
        self, symbol: str, depth: int = 5
    ) -> OrderBookSnapshot:
        client = self._require_client()
        raw = await client.fetch_order_book(symbol, limit=depth)

        bids = [
            OrderBookLevel(price=float(p), quantity=float(q))
            for p, q in (raw.get("bids") or [])[:depth]
        ]
        asks = [
            OrderBookLevel(price=float(p), quantity=float(q))
            for p, q in (raw.get("asks") or [])[:depth]
        ]
        ts = raw.get("timestamp") or int(time.time() * 1000)

        return OrderBookSnapshot(
            exchange=self._exchange_id,
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp_ms=int(ts),
        )

    async def fetch_balances(self) -> Dict[str, float]:
        client = self._require_client()
        raw = await client.fetch_balance()
        free: dict = raw.get("free", {})
        return {
            currency: float(amount)
            for currency, amount in free.items()
            if float(amount) > 0
        }

    async def place_market_order(
        self, symbol: str, side: str, quantity: float
    ) -> TradeResult:
        client = self._require_client()
        t0 = time.monotonic()

        try:
            order = await client.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=quantity,
            )
            latency_ms = (time.monotonic() - t0) * 1000

            filled = float(order.get("filled", 0))
            cost = float(order.get("cost", 0))
            avg_price = (cost / filled) if filled > 0 else 0.0

            fee_info = order.get("fee") or {}
            fee_cost = float(fee_info.get("cost", 0))
            fee_currency = fee_info.get("currency", "")

            return TradeResult(
                exchange=self._exchange_id,
                symbol=symbol,
                side=side,
                requested_qty=quantity,
                filled_qty=filled,
                avg_fill_price=avg_price,
                fee=fee_cost,
                fee_currency=fee_currency,
                latency_ms=latency_ms,
                order_id=str(order.get("id", "")),
                success=True,
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            logger.error(
                "%s order failed: %s %s %.6f — %s",
                self._exchange_id, side, symbol, quantity, exc,
            )
            return TradeResult(
                exchange=self._exchange_id,
                symbol=symbol,
                side=side,
                requested_qty=quantity,
                filled_qty=0.0,
                avg_fill_price=0.0,
                fee=0.0,
                fee_currency="",
                latency_ms=latency_ms,
                order_id="",
                success=False,
                error=str(exc),
            )

    async def fetch_trading_fee(self, symbol: str) -> float:
        if symbol in self._fee_cache:
            return self._fee_cache[symbol]

        client = self._require_client()
        try:
            fee_struct = await client.fetch_trading_fee(symbol)
            taker = float(fee_struct.get("taker", 0.001))
        except Exception:
            # Fallback: most exchanges default to 0.1 % taker
            logger.warning(
                "%s: could not fetch fee for %s — using 0.1%% fallback",
                self._exchange_id, symbol,
            )
            taker = 0.001

        self._fee_cache[symbol] = taker
        return taker

    # ── Helpers ──────────────────────────────────────────

    def _require_client(self) -> ccxt_async.Exchange:
        if self._client is None:
            raise RuntimeError(
                f"{self._exchange_id}: connect() must be called before any I/O"
            )
        return self._client
