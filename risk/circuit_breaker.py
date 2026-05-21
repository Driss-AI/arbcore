"""
Circuit Breaker — Hard Kill-Switch System
==========================================
Monitors real-time health signals and triggers an immediate halt when
critical thresholds are breached.

From the research report:
    "During flash crash events, MEXC's WebSocket architecture has been
    observed to freeze, drop packets, or interpolate data poorly …
    synchronized downtime across interconnected platforms during
    volatility spikes prevents automated risk management bots from
    accessing the API to close underwater positions."

Trip conditions (any one triggers HALT):
    1. Consecutive API failures exceed threshold
    2. Order placement latency exceeds ceiling (exchange is degraded)
    3. WebSocket heartbeat missed > 2× expected interval
    4. Any unhedged position open beyond max hold time
    5. Feed desync detected (REST vs WS mid-price diverges > threshold)

Once tripped, the breaker:
    a. Logs a CRITICAL alert with the trip reason
    b. Sets a flag that the engine reads on the next tick
    c. The engine must cancel all open orders and halt the loop
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CircuitBreakerConfig:
    """Tunables for the circuit breaker."""
    max_consecutive_api_failures: int = 3
    max_order_latency_ms: float = 5000.0
    max_ws_silence_s: float = 20.0      # 2× a typical 10s heartbeat
    max_unhedged_hold_s: float = 30.0
    max_desync_feeds: int = 2           # trip if ≥N feeds are desynced
    cooldown_s: float = 60.0            # min time before auto-reset


@dataclass
class TripEvent:
    """Record of a circuit breaker trip."""
    reason: str
    timestamp: float
    details: str = ""


class CircuitBreaker:
    """
    Real-time health monitor with kill-switch capability.

    The engine calls ``check()`` at the start of each tick.
    External systems feed signals via ``record_*`` methods.
    """

    def __init__(self, config: Optional[CircuitBreakerConfig] = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._tripped = False
        self._trip_history: List[TripEvent] = []
        self._last_trip_time: float = 0.0

        # Signal accumulators
        self._consecutive_api_failures: int = 0
        self._last_ws_data_time: float = time.monotonic()
        self._last_order_latency_ms: float = 0.0
        self._unhedged_since: Optional[float] = None
        self._desynced_feed_count: int = 0

    # ── Public API ───────────────────────────────────────

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    @property
    def trip_history(self) -> List[TripEvent]:
        return self._trip_history.copy()

    def check(self) -> Optional[str]:
        """
        Run all trip checks. Returns the trip reason string,
        or None if the system is healthy.
        """
        if self._tripped:
            return self._trip_history[-1].reason if self._trip_history else "tripped"

        # 1. Consecutive API failures
        if self._consecutive_api_failures >= self._config.max_consecutive_api_failures:
            return self._trip(
                f"consecutive_api_failures={self._consecutive_api_failures}",
                f"Exceeded threshold of {self._config.max_consecutive_api_failures}"
            )

        # 2. Order latency ceiling
        if self._last_order_latency_ms > self._config.max_order_latency_ms:
            return self._trip(
                f"order_latency={self._last_order_latency_ms:.0f}ms",
                f"Exceeded ceiling of {self._config.max_order_latency_ms:.0f}ms"
            )

        # 3. WebSocket silence
        ws_silence = time.monotonic() - self._last_ws_data_time
        if ws_silence > self._config.max_ws_silence_s:
            return self._trip(
                f"ws_silence={ws_silence:.1f}s",
                f"No WS data for {ws_silence:.1f}s (max={self._config.max_ws_silence_s}s)"
            )

        # 4. Unhedged position hold time
        if self._unhedged_since is not None:
            hold_time = time.monotonic() - self._unhedged_since
            if hold_time > self._config.max_unhedged_hold_s:
                return self._trip(
                    f"unhedged_hold={hold_time:.1f}s",
                    f"Unhedged position open for {hold_time:.1f}s"
                )

        # 5. Feed desync count
        if self._desynced_feed_count >= self._config.max_desync_feeds:
            return self._trip(
                f"desynced_feeds={self._desynced_feed_count}",
                f"{self._desynced_feed_count} feeds desynced"
            )

        return None

    def reset(self) -> bool:
        """
        Attempt to reset the breaker after cooldown.
        Returns True if reset was successful.
        """
        if not self._tripped:
            return True

        elapsed = time.monotonic() - self._last_trip_time
        if elapsed < self._config.cooldown_s:
            logger.warning(
                "Circuit breaker reset denied — %.0fs remaining in cooldown",
                self._config.cooldown_s - elapsed,
            )
            return False

        self._tripped = False
        self._consecutive_api_failures = 0
        self._last_order_latency_ms = 0.0
        self._unhedged_since = None
        self._desynced_feed_count = 0
        logger.info("Circuit breaker RESET — trading may resume.")
        return True

    # ── Signal inputs (called by engine / executor) ──────

    def record_api_success(self) -> None:
        self._consecutive_api_failures = 0

    def record_api_failure(self) -> None:
        self._consecutive_api_failures += 1

    def record_ws_data(self) -> None:
        self._last_ws_data_time = time.monotonic()

    def record_order_latency(self, latency_ms: float) -> None:
        self._last_order_latency_ms = latency_ms

    def record_unhedged_open(self) -> None:
        if self._unhedged_since is None:
            self._unhedged_since = time.monotonic()

    def record_unhedged_closed(self) -> None:
        self._unhedged_since = None

    def update_desync_count(self, count: int) -> None:
        self._desynced_feed_count = count

    # ── Private ──────────────────────────────────────────

    def _trip(self, reason: str, details: str) -> str:
        self._tripped = True
        self._last_trip_time = time.monotonic()
        event = TripEvent(reason=reason, timestamp=time.time(), details=details)
        self._trip_history.append(event)
        logger.critical("⚠ CIRCUIT BREAKER TRIPPED | %s | %s", reason, details)
        return reason
