"""
WebSocket Order Book Manager
=============================
Maintains a live, in-memory cache of order-book snapshots by subscribing
to each exchange's WebSocket streams via ``ccxt.pro``'s ``watch_order_book``.

The engine reads from this cache on every tick instead of firing REST calls,
cutting per-tick latency from 200-500ms (REST round-trip) to ~0ms (local read).

Architecture:
    • One background ``asyncio.Task`` per (exchange, symbol) pair.
    • Each task loops on ``watch_order_book()``, updating a shared dict.
    • The dict is read by the engine's tick loop (same event loop, no locks needed).
    • Tasks auto-reconnect on transient WebSocket failures with exponential backoff.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from exchanges.base import OrderBookLevel, OrderBookSnapshot
from utils.logger import get_logger

if TYPE_CHECKING:
    from exchanges.ccxt_adapter import CCXTAdapter

logger = get_logger(__name__)

_MAX_BACKOFF_S = 30.0
_INITIAL_BACKOFF_S = 1.0
_STALE_THRESHOLD_MS = 10_000  # book older than 10s is considered stale


class WebSocketManager:
    """
    Background order-book cache fed by WebSocket streams.

    Usage::

        ws = WebSocketManager(adapters, symbols)
        await ws.start()          # spawns background tasks
        snap = ws.get_snapshot("binance", "BTC/USDT")  # instant read
        await ws.stop()           # cancel all tasks
    """

    def __init__(
        self,
        adapters: Dict[str, "CCXTAdapter"],
        symbols: List[str],
        book_depth: int = 10,
    ) -> None:
        self._adapters = adapters
        self._symbols = symbols
        self._depth = book_depth

        # {exchange: {symbol: OrderBookSnapshot}}
        self._cache: Dict[str, Dict[str, OrderBookSnapshot]] = {}
        self._tasks: List[asyncio.Task] = []
        self._running = False

        # Track connection health
        self._connected_streams: Set[str] = set()
        self._error_counts: Dict[str, int] = {}

    # ── Public API ───────────────────────────────────────

    async def start(self) -> None:
        """Spawn one background task per (exchange, symbol)."""
        self._running = True
        for ex_name in self._adapters:
            self._cache[ex_name] = {}
            for symbol in self._symbols:
                task = asyncio.create_task(
                    self._stream_loop(ex_name, symbol),
                    name=f"ws_{ex_name}_{symbol}",
                )
                self._tasks.append(task)

        logger.info(
            "WebSocket manager started — %d streams across %d exchanges",
            len(self._tasks), len(self._adapters),
        )

    async def stop(self) -> None:
        """Cancel all background tasks and wait for them to finish."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("WebSocket manager stopped.")

    def get_snapshot(
        self, exchange: str, symbol: str
    ) -> Optional[OrderBookSnapshot]:
        """
        Read the latest cached snapshot.

        Returns None if the stream hasn't delivered data yet.
        """
        return self._cache.get(exchange, {}).get(symbol)

    def get_all_snapshots(self) -> Dict[str, Dict[str, OrderBookSnapshot]]:
        """
        Return the full cache in the same shape the engine expects:
        ``{exchange: {symbol: OrderBookSnapshot}}``.

        Filters out stale entries.
        """
        now_ms = int(time.time() * 1000)
        result: Dict[str, Dict[str, OrderBookSnapshot]] = {}

        for ex, books in self._cache.items():
            for sym, snap in books.items():
                age_ms = now_ms - snap.timestamp_ms
                if age_ms > _STALE_THRESHOLD_MS:
                    logger.debug(
                        "Stale book dropped: %s/%s (age=%dms)", ex, sym, age_ms
                    )
                    continue
                result.setdefault(ex, {})[sym] = snap

        return result

    @property
    def stream_count(self) -> int:
        """Number of currently active streams."""
        return len(self._connected_streams)

    @property
    def is_healthy(self) -> bool:
        """True if at least one stream is delivering data."""
        return len(self._connected_streams) > 0

    # ── Background stream loop ───────────────────────────

    async def _stream_loop(self, exchange: str, symbol: str) -> None:
        """
        Subscribe to order-book updates and keep the cache fresh.
        Reconnects with exponential backoff on failure.
        """
        stream_key = f"{exchange}/{symbol}"
        backoff = _INITIAL_BACKOFF_S
        adapter = self._adapters[exchange]

        while self._running:
            try:
                client = adapter._require_client()

                while self._running:
                    raw = await client.watch_order_book(symbol, limit=self._depth)
                    snap = self._normalise(exchange, symbol, raw)
                    self._cache[exchange][symbol] = snap

                    # Mark as connected on first successful read
                    if stream_key not in self._connected_streams:
                        self._connected_streams.add(stream_key)
                        logger.info("WS connected: %s", stream_key)

                    # Reset backoff on success
                    backoff = _INITIAL_BACKOFF_S

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._connected_streams.discard(stream_key)
                self._error_counts[stream_key] = (
                    self._error_counts.get(stream_key, 0) + 1
                )
                logger.warning(
                    "WS error %s (attempt #%d): %s — retrying in %.1fs",
                    stream_key,
                    self._error_counts[stream_key],
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)

        self._connected_streams.discard(stream_key)

    # ── Helpers ──────────────────────────────────────────

    @staticmethod
    def _normalise(
        exchange: str, symbol: str, raw: dict
    ) -> OrderBookSnapshot:
        """Convert raw ccxt order-book dict to our frozen dataclass."""
        bids = [
            OrderBookLevel(price=float(p), quantity=float(q))
            for p, q in (raw.get("bids") or [])
        ]
        asks = [
            OrderBookLevel(price=float(p), quantity=float(q))
            for p, q in (raw.get("asks") or [])
        ]
        ts = raw.get("timestamp") or int(time.time() * 1000)

        return OrderBookSnapshot(
            exchange=exchange,
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp_ms=int(ts),
        )
