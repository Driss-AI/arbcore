"""
Oracle Price Feed — Pyth Network Integration
=============================================
Fetches reference prices and Confidence Intervals from the Pyth Network
Hermes API to distinguish real stale quotes from normal market noise.

From the research report:
    "Sophisticated arbitrage bots use the Pyth CI as an algorithmic
    threshold filter. If a Tier-2 exchange's price deviates from the
    Pyth composite price within the bounds of the CI, the algorithm
    ignores it as standard market noise. However, if the CEX price
    deviates outside the established CI, it acts as mathematical
    confirmation of a true latency window or a stale quote."

The oracle is queried asynchronously via the Pyth Hermes REST API.
No API key is required for the public endpoint.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional

import httpx

from utils.logger import get_logger

logger = get_logger(__name__)

# Pyth price feed IDs for major pairs (mainnet)
# Full list: https://pyth.network/developers/price-feed-ids
_PYTH_FEED_IDS: Dict[str, str] = {
    "BTC/USDT": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "ETH/USDT": "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "SOL/USDT": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "XRP/USDT": "ec5d399846a9209f3fe5881d70aae9268c94339ff9817c8d9558fd4c9d5fb0ec",
    "DOGE/USDT": "dcef50dd0a4cd2dcc17e45df1676dcb336a11a61c69df7a0299b0150c672d25c",
    "AVAX/USDT": "93da3352f9f1d105fdfe4971cfa80e9dd777bfc5d0f683ebb6e1294b92137bb7",
    "LINK/USDT": "8ac0c70fff57e9aefdf5edf44b51d62c2d433653cbb2cf5cc06bb115af04d221",
    "ADA/USDT": "2a01deaec9e51a579277b34b122399984d0bbf57e2458a7e42fecd2829867a0d",
    "DOT/USDT": "ca3eed9ab553a5c741a4e568ef054399e2b1e0d8e87e6eebc2b1b640e4dda1e0",
    "LTC/USDT": "6e3f3fa8253588df9326580180233eb791e03b443a3ba7a1d892e73874e19a54",
}

_HERMES_BASE_URL = "https://hermes.pyth.network"


@dataclass(frozen=True)
class OraclePrice:
    """A single oracle price observation with confidence."""
    symbol: str
    price: float
    confidence: float       # absolute confidence interval (same units as price)
    confidence_pct: float   # confidence as a percentage of price
    timestamp_ms: int
    status: str             # "trading" | "unknown" | "halted"


class OracleClient:
    """
    Async client for the Pyth Network Hermes API.

    Usage::

        oracle = OracleClient()
        await oracle.start()
        price = oracle.get_price("BTC/USDT")
        if price and price.confidence_pct < 2.0:
            # Oracle is confident — safe to use as benchmark
            ...
        await oracle.stop()
    """

    def __init__(
        self,
        max_ci_pct: float = 2.0,
        refresh_interval_s: float = 2.0,
    ) -> None:
        self._max_ci_pct = max_ci_pct
        self._refresh_interval_s = refresh_interval_s
        self._cache: Dict[str, OraclePrice] = {}
        self._last_fetch: float = 0.0
        self._http: Optional[httpx.AsyncClient] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._running = False

    # ── Lifecycle ────────────────────────────────────────

    async def start(self) -> None:
        """Open HTTP client and begin background refresh loop."""
        self._http = httpx.AsyncClient(timeout=5.0)
        self._running = True
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="oracle_refresh"
        )
        logger.info("Oracle client started — tracking %d feeds", len(_PYTH_FEED_IDS))

    async def stop(self) -> None:
        """Cancel refresh and close HTTP."""
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._http:
            await self._http.aclose()
        logger.info("Oracle client stopped.")

    # ── Public API ───────────────────────────────────────

    def get_price(self, symbol: str) -> Optional[OraclePrice]:
        """Return cached oracle price for a symbol, or None."""
        return self._cache.get(symbol)

    def is_divergence_significant(
        self,
        symbol: str,
        cex_price: float,
    ) -> Optional[bool]:
        """
        Determine if a CEX price represents a true stale quote.

        Returns
        -------
        True   — CEX deviates *outside* the oracle CI → confirmed stale quote
        False  — CEX is within CI → normal noise, ignore
        None   — no oracle data available or CI too wide
        """
        oracle = self._cache.get(symbol)
        if oracle is None:
            return None

        # If oracle CI is too wide, market is too uncertain to trade
        if oracle.confidence_pct > self._max_ci_pct:
            return None

        deviation = abs(cex_price - oracle.price) / oracle.price
        return deviation > oracle.confidence_pct / 100.0

    @property
    def feed_count(self) -> int:
        return len(self._cache)

    # ── Background refresh ───────────────────────────────

    async def _refresh_loop(self) -> None:
        """Periodically fetch latest prices from Pyth."""
        while self._running:
            try:
                await self._fetch_prices()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Oracle fetch failed: %s", exc)
            await asyncio.sleep(self._refresh_interval_s)

    async def _fetch_prices(self) -> None:
        """Batch-fetch all tracked feed IDs from Hermes."""
        if not self._http:
            return

        feed_ids = list(_PYTH_FEED_IDS.values())
        params = [("ids[]", fid) for fid in feed_ids]

        resp = await self._http.get(
            f"{_HERMES_BASE_URL}/v2/updates/price/latest",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

        # Reverse map: feed_id → symbol
        id_to_symbol = {v: k for k, v in _PYTH_FEED_IDS.items()}

        for entry in data.get("parsed", []):
            feed_id = entry.get("id", "")
            symbol = id_to_symbol.get(feed_id)
            if not symbol:
                continue

            price_data = entry.get("price", {})
            price_val = int(price_data.get("price", 0))
            conf_val = int(price_data.get("conf", 0))
            expo = int(price_data.get("expo", 0))
            publish_time = int(price_data.get("publish_time", 0))

            price_float = price_val * (10 ** expo)
            conf_float = conf_val * (10 ** expo)

            if price_float <= 0:
                continue

            conf_pct = (conf_float / price_float) * 100.0

            self._cache[symbol] = OraclePrice(
                symbol=symbol,
                price=price_float,
                confidence=conf_float,
                confidence_pct=conf_pct,
                timestamp_ms=publish_time * 1000,
                status="trading",
            )

        self._last_fetch = time.monotonic()
        logger.debug("Oracle refreshed — %d prices cached", len(self._cache))
