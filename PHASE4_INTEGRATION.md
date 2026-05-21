# Phase 4 Integration Guide — Report-Driven Hardening

## Overview

This upgrade adds 7 new modules that address specific risks identified in the
**Quantitative Analysis of Latency Arbitrage** research report. The existing
codebase requires targeted modifications to wire them in.

---

## New Files (copy into repo root)

```
data/__init__.py           ← empty module init
data/oracle.py             ← Pyth Network CI benchmark
data/feed_reconciler.py    ← REST vs WS book integrity checker
risk/circuit_breaker.py    ← hard kill-switch system
risk/rate_limiter.py       ← token-bucket per-exchange throttling
strategies/filters.py      ← oracle CI + feed health + freshness gates
utils/latency_tracker.py   ← execution histogram for ALP detection
requirements.txt           ← replace (adds httpx==0.28.1)
```

---

## Modifications to Existing Files

### 1. `config/settings.py` — Add these new fields

Add to the `RiskLimits` dataclass:

```python
@dataclass(frozen=True)
class RiskLimits:
    # ... existing fields ...
    max_trade_usd: float = 100.0
    max_drawdown_pct: float = 2.0
    max_slippage_pct: float = 0.15
    max_open_exposure_usd: float = 500.0
    daily_loss_limit_usd: float = 50.0

    # ── Phase 4: Circuit Breaker ──────────────────────────
    max_consecutive_api_failures: int = 3
    max_order_latency_ms: float = 5000.0
    max_ws_silence_s: float = 20.0
    max_unhedged_hold_s: float = 30.0
    circuit_breaker_cooldown_s: float = 60.0
```

Add to the `Settings` dataclass:

```python
@dataclass(frozen=True)
class Settings:
    # ... existing fields ...

    # ── Phase 4: Oracle & Tier-2 config ───────────────────
    use_oracle: bool = False
    oracle_max_ci_pct: float = 2.0
    oracle_refresh_interval_s: float = 2.0
    feed_reconcile_interval_s: float = 30.0
    feed_desync_threshold_pct: float = 0.5
```

Add to `Settings.load()`:

```python
    use_oracle=os.getenv("ARBCORE_USE_ORACLE", "false").lower() == "true",
    oracle_max_ci_pct=float(os.getenv("ARBCORE_ORACLE_MAX_CI_PCT", "2.0")),
    oracle_refresh_interval_s=float(os.getenv("ARBCORE_ORACLE_REFRESH_S", "2.0")),
    feed_reconcile_interval_s=float(os.getenv("ARBCORE_FEED_RECONCILE_S", "30.0")),
    feed_desync_threshold_pct=float(os.getenv("ARBCORE_FEED_DESYNC_PCT", "0.5")),
```

Update the default exchange list for Tier-2 targeting:

```python
    enabled = [
        e.strip().lower()
        for e in os.getenv("ARBCORE_EXCHANGES", "binance,kucoin").split(",")
        if e.strip()
    ]
```

### 2. `core/engine.py` — Wire in circuit breaker, oracle, filters, reconciler

Add imports at the top:

```python
from data.oracle import OracleClient
from data.feed_reconciler import FeedReconciler
from risk.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from risk.rate_limiter import create_rate_limiters
from strategies.filters import filter_by_oracle_ci, filter_by_feed_health
from utils.latency_tracker import LatencyTracker
```

Add new instance variables in `Engine.__init__`:

```python
    self._circuit_breaker: Optional[CircuitBreaker] = None
    self._oracle: Optional[OracleClient] = None
    self._reconciler: Optional[FeedReconciler] = None
    self._latency_tracker: Optional[LatencyTracker] = None
    self._rate_limiters: Dict[str, Any] = {}
```

In `Engine.start()`, after building the risk manager (step 5), add:

```python
    # ── 5b. Build circuit breaker ────────────────────
    self._circuit_breaker = CircuitBreaker(CircuitBreakerConfig(
        max_consecutive_api_failures=self._settings.risk.max_consecutive_api_failures,
        max_order_latency_ms=self._settings.risk.max_order_latency_ms,
        max_ws_silence_s=self._settings.risk.max_ws_silence_s,
        max_unhedged_hold_s=self._settings.risk.max_unhedged_hold_s,
        cooldown_s=self._settings.risk.circuit_breaker_cooldown_s,
    ))

    # ── 5c. Build rate limiters ──────────────────────
    self._rate_limiters = create_rate_limiters(self._settings.enabled_exchanges)

    # ── 5d. Build latency tracker ────────────────────
    self._latency_tracker = LatencyTracker()

    # ── 5e. Start oracle (if enabled) ────────────────
    if self._settings.use_oracle:
        self._oracle = OracleClient(
            max_ci_pct=self._settings.oracle_max_ci_pct,
            refresh_interval_s=self._settings.oracle_refresh_interval_s,
        )
        await self._oracle.start()
        logger.info("Oracle integration active — Pyth CI filter enabled")

    # ── 5f. Start feed reconciler (if WS mode) ──────
    if self._ws_manager:
        self._reconciler = FeedReconciler(
            adapters=self._adapters,
            ws_manager=self._ws_manager,
            symbols=self._symbols,
            interval_s=self._settings.feed_reconcile_interval_s,
            divergence_threshold_pct=self._settings.feed_desync_threshold_pct,
        )
        await self._reconciler.start()
```

In `Engine._tick()`, add circuit breaker check at the very top:

```python
    async def _tick(self) -> None:
        self._tick_count += 1

        # ── 0. Circuit breaker gate ──────────────────
        if self._circuit_breaker:
            # Feed desync count into breaker
            if self._reconciler:
                self._circuit_breaker.update_desync_count(
                    len(self._reconciler.desynced_feeds)
                )
            trip_reason = self._circuit_breaker.check()
            if trip_reason:
                logger.critical("CIRCUIT BREAKER ACTIVE — skipping tick: %s", trip_reason)
                self._shutdown_requested = True
                return

        # ... rest of existing tick code ...
```

After strategy scanning (step 3), add the filters:

```python
    # ── 3b. Apply Phase 4 filters ────────────────────
    all_opportunities = filter_by_oracle_ci(all_opportunities, self._oracle)
    if self._reconciler:
        all_opportunities = filter_by_feed_health(
            all_opportunities, self._reconciler.desynced_feeds
        )
```

After execution (step 4), feed latency data:

```python
    # After each execution report:
    if self._latency_tracker:
        for fill in report.fills:
            if fill.success:
                self._latency_tracker.record(fill.exchange, fill.latency_ms)

    if self._circuit_breaker:
        for fill in report.fills:
            if fill.success:
                self._circuit_breaker.record_api_success()
                self._circuit_breaker.record_order_latency(fill.latency_ms)
            else:
                self._circuit_breaker.record_api_failure()
        if report.unhedged:
            self._circuit_breaker.record_unhedged_open()
```

In `Engine.shutdown()`, add cleanup for new services:

```python
    if self._oracle:
        await self._oracle.stop()
    if self._reconciler:
        await self._reconciler.stop()
```

### 3. `.env.example` — Add new environment variables

```bash
# ── Phase 4: Oracle & Hardening ───────────────────────────
ARBCORE_USE_ORACLE=false
ARBCORE_ORACLE_MAX_CI_PCT=2.0
ARBCORE_ORACLE_REFRESH_S=2.0
ARBCORE_FEED_RECONCILE_S=30.0
ARBCORE_FEED_DESYNC_PCT=0.5

# ── Phase 4: Tier-2 Exchange Credentials ──────────────────
KUCOIN_API_KEY=
KUCOIN_API_SECRET=
KUCOIN_PASSPHRASE=
GATEIO_API_KEY=
GATEIO_API_SECRET=
MEXC_API_KEY=
MEXC_API_SECRET=
```

---

## How Each Module Maps to the Report

| Module                   | Report Risk                                       | Defense                                    |
|--------------------------|---------------------------------------------------|--------------------------------------------|
| `risk/circuit_breaker.py`| MEXC API freezes during flash crashes              | Hard kill-switch on WS death/API failures  |
| `risk/rate_limiter.py`   | Burst API calls → one-leg-filled exposure          | Token-bucket per-exchange throttling       |
| `data/oracle.py`         | Noise vs real stale quotes indistinguishable       | Pyth CI as mathematical confirmation       |
| `data/feed_reconciler.py`| KuCoin no WS snapshot → 30s drift                 | Periodic REST validation of WS cache       |
| `strategies/filters.py`  | Sub-1.5% edges eaten by fees + noise               | Oracle CI + feed health pre-execution gate |
| `utils/latency_tracker.py`| ALP throttling creates unnatural latency floors   | Histogram analysis detects clustering      |

---

## Recommended Railway Environment Variables for Tier-2 Strategy

```
ARBCORE_MODE=dry_run
ARBCORE_EXCHANGES=binance,kucoin
ARBCORE_STRATEGY=spatial
ARBCORE_USE_WEBSOCKET=true
ARBCORE_CONCURRENT_EXEC=true
ARBCORE_USE_ORACLE=true
ARBCORE_MAX_TRADE_USD=50.0
ARBCORE_DAILY_LOSS_LIMIT_USD=25.0
ARBCORE_MAX_DRAWDOWN_PCT=2.0
```

Start in `dry_run` mode. Monitor logs for opportunity frequency and net spreads.
Only switch to `live` after confirming consistent positive-net-spread detections.
