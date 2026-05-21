"""
Token-Bucket Rate Limiter
=========================
Per-exchange rate limiter that prevents API call bursts.

While ccxt has built-in rate limiting via ``enableRateLimit``, it is
cooperative and cannot prevent burst scenarios during high-volatility
periods when multiple concurrent tasks fire orders simultaneously.

A rate limit violation during active arbitrage execution is a
*position risk event* — you could have one leg filled and be unable
to place the second leg.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict

from utils.logger import get_logger

logger = get_logger(__name__)


class TokenBucketLimiter:
    """
    Async token-bucket rate limiter.

    Parameters
    ----------
    max_requests : bucket capacity (burst limit)
    per_seconds  : refill window in seconds
    """

    def __init__(self, max_requests: int, per_seconds: float) -> None:
        self._max_tokens = float(max_requests)
        self._tokens = float(max_requests)
        self._refill_rate = max_requests / per_seconds
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """
        Wait until a token is available, then consume one.
        Returns the number of seconds waited (0.0 if immediate).
        """
        async with self._lock:
            self._refill()
            waited = 0.0

            if self._tokens < 1.0:
                deficit = 1.0 - self._tokens
                wait_time = deficit / self._refill_rate
                waited = wait_time
                await asyncio.sleep(wait_time)
                self._refill()

            self._tokens -= 1.0
            return waited

    @property
    def available_tokens(self) -> float:
        self._refill()
        return self._tokens

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._max_tokens,
            self._tokens + elapsed * self._refill_rate,
        )
        self._last_refill = now


# Conservative defaults based on public API docs.
_EXCHANGE_LIMITS: Dict[str, tuple] = {
    "binance":  (1200, 60),  # 1200 req/min
    "kucoin":   (200, 10),   # ~20 req/s
    "gateio":   (300, 10),   # ~30 req/s
    "gate":     (300, 10),   # alias
    "mexc":     (200, 10),   # ~20 req/s
    "kraken":   (60, 60),    # 60 req/min
    "okx":      (600, 60),   # 600 req/min
}


def create_rate_limiters(
    exchange_ids: list[str],
) -> Dict[str, TokenBucketLimiter]:
    """Create a rate limiter for each enabled exchange."""
    limiters: Dict[str, TokenBucketLimiter] = {}
    for ex_id in exchange_ids:
        max_req, per_sec = _EXCHANGE_LIMITS.get(ex_id, (120, 60))
        limiters[ex_id] = TokenBucketLimiter(max_req, per_sec)
        logger.info("Rate limiter for %s: %d requests per %ds", ex_id, max_req, per_sec)
    return limiters
