"""
Feed Reconciler — REST vs WebSocket Book Integrity
===================================================
Periodically fetches a REST order-book snapshot and compares it against
the live WebSocket cache to detect silent desynchronisation.

From the research report:
    "KuCoin fails to serve a WebSocket snapshot upon initial connection,
    forcing client architectures to fetch a REST snapshot before
    attempting to synchronize the stream — a process that introduces
    inherent desynchronization if the order book is experiencing low
    delta updates."

Also relevant for Gate.io's 50-100ms batching lag and MEXC packet drops.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from utils.logger import get_logger

if TYPE_CHECKING:
    from exchanges.base import BaseExchangeAdapter, OrderBookSnapshot
    from exchanges.ws_manager import WebSocketManager

logger = get_logger(__name__)


@dataclass(frozen=True)
class DesyncAlert:
    """A detected feed desynchronisation event."""
    exchange: str
    symbol: str
    ws_mid: float
    rest_mid: float
    divergence_pct: float
    timestamp_ms: int


class FeedReconciler:
    """
    Compares WebSocket cache against periodic REST snapshots.

    Parameters
    ----------
    adapters         : exchange name → connected adapter
    ws_manager       : the live WebSocket manager
    symbols          : symbols to reconcile
    interval_s       : seconds between reconciliation sweeps
    divergence_threshold_pct : max allowed mid-price divergence
    """

    def __init__(
        self,
        adapters: Dict[str, "BaseExchangeAdapter"],
        ws_manager: "WebSocketManager",
        symbols: List[str],
        interval_s: float = 30.0,
        divergence_threshold_pct: float = 0.5,
    ) -> None:
        self._adapters = adapters
        self._ws_manager = ws_manager
        self._symbols = symbols
        self._interval_s = interval_s
        self._threshold = divergence_threshold_pct

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._desynced_feeds: Set[str] = set()
        self._alert_history: List[DesyncAlert] = []

    # ── Lifecycle ────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(
            self._reconcile_loop(), name="feed_reconciler"
        )
        logger.info(
            "Feed reconciler started — interval=%.0fs threshold=%.2f%%",
            self._interval_s, self._threshold,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Feed reconciler stopped.")

    # ── Public API ───────────────────────────────────────

    @property
    def desynced_feeds(self) -> Set[str]:
        """Set of 'exchange/symbol' keys currently flagged as desynced."""
        return self._desynced_feeds.copy()

    def is_feed_healthy(self, exchange: str, symbol: str) -> bool:
        """Returns False if this feed is currently flagged as desynced."""
        return f"{exchange}/{symbol}" not in self._desynced_feeds

    @property
    def alert_count(self) -> int:
        return len(self._alert_history)

    # ── Background loop ──────────────────────────────────

    async def _reconcile_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._interval_s)
            try:
                await self._reconcile_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Reconciliation sweep failed: %s", exc)

    async def _reconcile_all(self) -> None:
        """Compare every WS-cached book against a fresh REST fetch."""
        cleared: List[str] = []

        for ex_name, adapter in self._adapters.items():
            for symbol in self._symbols:
                ws_snap = self._ws_manager.get_snapshot(ex_name, symbol)
                if ws_snap is None:
                    continue

                ws_mid = self._mid_price(ws_snap)
                if ws_mid is None:
                    continue

                try:
                    rest_snap = await adapter.fetch_order_book(symbol, depth=5)
                except Exception:
                    continue

                rest_mid = self._mid_price(rest_snap)
                if rest_mid is None:
                    continue

                divergence = abs(ws_mid - rest_mid) / rest_mid * 100
                key = f"{ex_name}/{symbol}"

                if divergence > self._threshold:
                    if key not in self._desynced_feeds:
                        alert = DesyncAlert(
                            exchange=ex_name,
                            symbol=symbol,
                            ws_mid=ws_mid,
                            rest_mid=rest_mid,
                            divergence_pct=divergence,
                            timestamp_ms=int(time.time() * 1000),
                        )
                        self._alert_history.append(alert)
                        self._desynced_feeds.add(key)
                        logger.warning(
                            "FEED_DESYNC | %s | ws_mid=%.2f rest_mid=%.2f div=%.3f%%",
                            key, ws_mid, rest_mid, divergence,
                        )
                else:
                    if key in self._desynced_feeds:
                        cleared.append(key)

        for key in cleared:
            self._desynced_feeds.discard(key)
            logger.info("FEED_RESYNC | %s back in sync", key)

    @staticmethod
    def _mid_price(snap: "OrderBookSnapshot") -> Optional[float]:
        if not snap.bids or not snap.asks:
            return None
        return (snap.bids[0].price + snap.asks[0].price) / 2.0
