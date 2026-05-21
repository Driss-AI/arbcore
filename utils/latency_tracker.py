"""
Latency Tracker — Execution Histogram for ALP Detection
========================================================
Records per-exchange order execution latencies and exposes statistical
summaries that can detect Anti-Latency Plugin (ALP) throttling.

From the research report:
    "If an execution time histogram reveals a hard, unnatural floor at
    a specific millisecond marker (e.g., a massive concentration of
    fills taking exactly 250ms), it serves as definitive proof that the
    account has been throttled by an ALP."
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_MAX_HISTORY = 500


@dataclass(frozen=True)
class LatencyStats:
    """Statistical summary of recent execution latencies."""
    exchange: str
    count: int
    mean_ms: float
    median_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    std_dev_ms: float
    suspected_floor_ms: Optional[float]


class LatencyTracker:
    """
    Per-exchange latency histogram.

    Usage::

        tracker = LatencyTracker()
        tracker.record("kucoin", 342.5)
        stats = tracker.get_stats("kucoin")
        if stats and stats.suspected_floor_ms:
            # ALP-like throttling detected
    """

    def __init__(self, max_history: int = _MAX_HISTORY) -> None:
        self._max_history = max_history
        self._history: Dict[str, deque] = {}

    def record(self, exchange: str, latency_ms: float) -> None:
        """Record a single execution latency observation."""
        if exchange not in self._history:
            self._history[exchange] = deque(maxlen=self._max_history)
        self._history[exchange].append((time.time(), latency_ms))

    def get_stats(self, exchange: str) -> Optional[LatencyStats]:
        """Compute statistical summary for an exchange."""
        hist = self._history.get(exchange)
        if not hist or len(hist) < 5:
            return None

        latencies = sorted([ms for _, ms in hist])
        n = len(latencies)

        mean = sum(latencies) / n
        median = latencies[n // 2]
        p95 = latencies[int(n * 0.95)]
        p99 = latencies[int(n * 0.99)]
        variance = sum((x - mean) ** 2 for x in latencies) / n
        std_dev = math.sqrt(variance)

        floor = self._detect_floor(latencies)

        return LatencyStats(
            exchange=exchange,
            count=n,
            mean_ms=mean,
            median_ms=median,
            p95_ms=p95,
            p99_ms=p99,
            min_ms=latencies[0],
            max_ms=latencies[-1],
            std_dev_ms=std_dev,
            suspected_floor_ms=floor,
        )

    def get_all_stats(self) -> Dict[str, LatencyStats]:
        result: Dict[str, LatencyStats] = {}
        for exchange in self._history:
            stats = self.get_stats(exchange)
            if stats:
                result[exchange] = stats
        return result

    def check_for_alp(
        self,
        exchange: str,
        cluster_pct_threshold: float = 0.40,
        window_ms: float = 20.0,
    ) -> Optional[float]:
        """
        Explicitly check if an exchange shows ALP-like throttling.
        Returns the suspected floor latency in ms, or None.
        """
        hist = self._history.get(exchange)
        if not hist or len(hist) < 20:
            return None
        latencies = sorted([ms for _, ms in hist])
        return self._detect_floor(latencies, cluster_pct=cluster_pct_threshold, window_ms=window_ms)

    @staticmethod
    def _detect_floor(
        sorted_latencies: List[float],
        cluster_pct: float = 0.40,
        window_ms: float = 20.0,
    ) -> Optional[float]:
        """
        Detect if >cluster_pct of latencies cluster within a narrow window.
        This is the signature of an ALP: a hard, artificial floor.
        """
        n = len(sorted_latencies)
        if n < 10:
            return None

        threshold_count = int(n * cluster_pct)
        left = 0
        max_count = 0
        max_floor = None

        for right in range(n):
            while sorted_latencies[right] - sorted_latencies[left] > window_ms:
                left += 1
            count = right - left + 1
            if count > max_count:
                max_count = count
                max_floor = sorted_latencies[left]

        if max_count >= threshold_count and max_floor is not None:
            logger.warning(
                "ALP_SUSPECT | %d/%d fills (%.0f%%) cluster near %.0fms",
                max_count, n, (max_count / n) * 100, max_floor,
            )
            return max_floor

        return None
