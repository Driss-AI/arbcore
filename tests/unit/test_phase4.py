"""
Tests for Phase 4 modules: circuit breaker, rate limiter, oracle,
feed reconciler, filters, and latency tracker.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Circuit Breaker Tests ────────────────────────────────

from risk.circuit_breaker import CircuitBreaker, CircuitBreakerConfig


class TestCircuitBreaker:
    def test_healthy_returns_none(self):
        cb = CircuitBreaker()
        assert cb.check() is None
        assert not cb.is_tripped

    def test_trips_on_consecutive_api_failures(self):
        cb = CircuitBreaker(CircuitBreakerConfig(max_consecutive_api_failures=3))
        cb.record_api_failure()
        cb.record_api_failure()
        assert cb.check() is None  # only 2
        cb.record_api_failure()
        assert cb.check() is not None
        assert cb.is_tripped

    def test_api_success_resets_counter(self):
        cb = CircuitBreaker(CircuitBreakerConfig(max_consecutive_api_failures=3))
        cb.record_api_failure()
        cb.record_api_failure()
        cb.record_api_success()
        cb.record_api_failure()
        assert cb.check() is None  # reset after success

    def test_trips_on_order_latency(self):
        cb = CircuitBreaker(CircuitBreakerConfig(max_order_latency_ms=5000))
        cb.record_order_latency(6000.0)
        assert cb.check() is not None

    def test_trips_on_desync_feeds(self):
        cb = CircuitBreaker(CircuitBreakerConfig(max_desync_feeds=2))
        cb.update_desync_count(1)
        assert cb.check() is None
        cb.update_desync_count(2)
        assert cb.check() is not None

    def test_stays_tripped_until_reset(self):
        cb = CircuitBreaker(CircuitBreakerConfig(
            max_consecutive_api_failures=1,
            cooldown_s=0.1,
        ))
        cb.record_api_failure()
        cb.check()
        assert cb.is_tripped

        # Can't reset before cooldown
        assert not cb.reset()

        # Wait for cooldown
        time.sleep(0.15)
        assert cb.reset()
        assert not cb.is_tripped

    def test_trip_history_recorded(self):
        cb = CircuitBreaker(CircuitBreakerConfig(max_consecutive_api_failures=1))
        cb.record_api_failure()
        cb.check()
        history = cb.trip_history
        assert len(history) == 1
        assert "api_failures" in history[0].reason


# ── Rate Limiter Tests ───────────────────────────────────

from risk.rate_limiter import TokenBucketLimiter, create_rate_limiters


class TestTokenBucketLimiter:
    @pytest.mark.asyncio
    async def test_immediate_acquire_when_tokens_available(self):
        limiter = TokenBucketLimiter(max_requests=10, per_seconds=1.0)
        waited = await limiter.acquire()
        assert waited == 0.0

    @pytest.mark.asyncio
    async def test_waits_when_depleted(self):
        limiter = TokenBucketLimiter(max_requests=1, per_seconds=10.0)
        await limiter.acquire()  # consume the one token
        t0 = time.monotonic()
        await limiter.acquire()  # must wait for refill
        elapsed = time.monotonic() - t0
        assert elapsed > 0.05  # waited at least some time

    def test_create_rate_limiters(self):
        limiters = create_rate_limiters(["binance", "kucoin"])
        assert "binance" in limiters
        assert "kucoin" in limiters
        assert limiters["binance"].available_tokens > 0


# ── Oracle Tests ─────────────────────────────────────────

from data.oracle import OracleClient, OraclePrice


class TestOracleClient:
    def test_is_divergence_significant_outside_ci(self):
        oracle = OracleClient(max_ci_pct=2.0)
        # Manually inject a cached price
        oracle._cache["BTC/USDT"] = OraclePrice(
            symbol="BTC/USDT",
            price=100000.0,
            confidence=500.0,   # 0.5% CI
            confidence_pct=0.5,
            timestamp_ms=int(time.time() * 1000),
            status="trading",
        )
        # 2% divergence is outside 0.5% CI → True
        assert oracle.is_divergence_significant("BTC/USDT", 98000.0) is True

    def test_is_divergence_significant_within_ci(self):
        oracle = OracleClient(max_ci_pct=2.0)
        oracle._cache["BTC/USDT"] = OraclePrice(
            symbol="BTC/USDT",
            price=100000.0,
            confidence=500.0,
            confidence_pct=0.5,
            timestamp_ms=int(time.time() * 1000),
            status="trading",
        )
        # 0.1% divergence is within 0.5% CI → False (noise)
        assert oracle.is_divergence_significant("BTC/USDT", 99900.0) is False

    def test_returns_none_when_ci_too_wide(self):
        oracle = OracleClient(max_ci_pct=2.0)
        oracle._cache["BTC/USDT"] = OraclePrice(
            symbol="BTC/USDT",
            price=100000.0,
            confidence=5000.0,
            confidence_pct=5.0,  # 5% CI exceeds 2% max
            timestamp_ms=int(time.time() * 1000),
            status="trading",
        )
        assert oracle.is_divergence_significant("BTC/USDT", 95000.0) is None

    def test_returns_none_when_no_data(self):
        oracle = OracleClient()
        assert oracle.is_divergence_significant("BTC/USDT", 100000.0) is None


# ── Filters Tests ────────────────────────────────────────

from strategies.filters import filter_by_oracle_ci, filter_by_feed_health
from strategies.base import Opportunity, TradeLeg


def _make_spatial_opp(buy_price: float = 100.0, symbol: str = "BTC/USDT") -> Opportunity:
    return Opportunity(
        strategy="spatial",
        legs=[
            TradeLeg(exchange="kucoin", symbol=symbol, side="buy",
                     price=buy_price, quantity=0.01, fee_rate=0.001),
            TradeLeg(exchange="binance", symbol=symbol, side="sell",
                     price=buy_price * 1.005, quantity=0.01, fee_rate=0.001),
        ],
        gross_pct=0.5,
        net_pct=0.3,
    )


def _make_triangular_opp() -> Opportunity:
    return Opportunity(
        strategy="triangular",
        legs=[
            TradeLeg(exchange="binance", symbol="BTC/USDT", side="buy",
                     price=100.0, quantity=0.01, fee_rate=0.001),
        ],
        gross_pct=0.5,
        net_pct=0.3,
    )


class TestOracleFilter:
    def test_passes_when_oracle_confirms(self):
        oracle = MagicMock()
        oracle.is_divergence_significant.return_value = True
        opps = [_make_spatial_opp()]
        result = filter_by_oracle_ci(opps, oracle)
        assert len(result) == 1

    def test_rejects_when_within_ci(self):
        oracle = MagicMock()
        oracle.is_divergence_significant.return_value = False
        opps = [_make_spatial_opp()]
        result = filter_by_oracle_ci(opps, oracle)
        assert len(result) == 0

    def test_passes_triangular_unfiltered(self):
        oracle = MagicMock()
        opps = [_make_triangular_opp()]
        result = filter_by_oracle_ci(opps, oracle)
        assert len(result) == 1

    def test_passes_all_when_no_oracle(self):
        opps = [_make_spatial_opp()]
        result = filter_by_oracle_ci(opps, None)
        assert len(result) == 1


class TestFeedHealthFilter:
    def test_rejects_desynced_feed(self):
        opps = [_make_spatial_opp()]
        desynced = {"kucoin/BTC/USDT"}
        result = filter_by_feed_health(opps, desynced)
        assert len(result) == 0

    def test_passes_healthy_feed(self):
        opps = [_make_spatial_opp()]
        result = filter_by_feed_health(opps, set())
        assert len(result) == 1


# ── Latency Tracker Tests ───────────────────────────────

from utils.latency_tracker import LatencyTracker


class TestLatencyTracker:
    def test_basic_stats(self):
        tracker = LatencyTracker()
        for i in range(20):
            tracker.record("kucoin", 200.0 + i)
        stats = tracker.get_stats("kucoin")
        assert stats is not None
        assert stats.count == 20
        assert stats.min_ms == 200.0
        assert stats.max_ms == 219.0
        assert stats.suspected_floor_ms is None  # no clustering

    def test_detects_alp_floor(self):
        tracker = LatencyTracker()
        # 80% of fills at exactly 250ms ± 5ms (ALP signature)
        for _ in range(40):
            tracker.record("mexc", 250.0 + (time.time() % 5))
        # 20% scattered
        for i in range(10):
            tracker.record("mexc", 100.0 + i * 50)

        floor = tracker.check_for_alp("mexc")
        assert floor is not None
        assert 240 <= floor <= 260

    def test_returns_none_with_insufficient_data(self):
        tracker = LatencyTracker()
        tracker.record("kucoin", 100.0)
        assert tracker.get_stats("kucoin") is None
