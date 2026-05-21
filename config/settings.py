"""
Centralised, immutable configuration for ArbCore.
==================================================
All secrets and tunables are read from environment variables (Railway)
or a local `.env` file (development).  Nothing is ever hardcoded.

Environment variables expected:
    # ── Exchange API credentials ──────────────────────────
    BINANCE_API_KEY / BINANCE_API_SECRET
    KRAKEN_API_KEY  / KRAKEN_API_SECRET
    OKX_API_KEY     / OKX_API_SECRET / OKX_PASSPHRASE
    KUCOIN_API_KEY  / KUCOIN_API_SECRET / KUCOIN_PASSPHRASE
    GATEIO_API_KEY  / GATEIO_API_SECRET
    MEXC_API_KEY    / MEXC_API_SECRET

    # ── Runtime tunables ──────────────────────────────────
    ARBCORE_MODE            dry_run | live        (default: dry_run)
    ARBCORE_EXCHANGES       comma-separated list  (default: binance,kucoin)
    ARBCORE_LOG_LEVEL       DEBUG | INFO | …      (default: INFO)
    ARBCORE_POLL_INTERVAL   seconds between ticks (default: 1.0)
    ARBCORE_STRATEGY        spatial | triangular | both  (default: spatial)
    ARBCORE_USE_WEBSOCKET   true | false          (default: true)
    ARBCORE_CONCURRENT_EXEC true | false          (default: true)
    ARBCORE_HEALTH_PORT     HTTP health port      (default: 8080)

    # ── Risk limits ───────────────────────────────────────
    ARBCORE_MAX_TRADE_USD          per-trade cap        (default: 100.0)
    ARBCORE_MAX_DRAWDOWN_PCT       kill-switch %        (default: 2.0)
    ARBCORE_MAX_SLIPPAGE_PCT       order-level cap      (default: 0.15)
    ARBCORE_MAX_OPEN_EXPOSURE_USD  aggregate position   (default: 500.0)
    ARBCORE_DAILY_LOSS_LIMIT_USD   hard daily stop      (default: 50.0)

    # ── Phase 4: Circuit Breaker ──────────────────────────
    ARBCORE_MAX_API_FAILURES       consecutive failures  (default: 3)
    ARBCORE_MAX_ORDER_LATENCY_MS   per-leg ceiling       (default: 5000)
    ARBCORE_MAX_WS_SILENCE_S       heartbeat timeout     (default: 20.0)
    ARBCORE_MAX_UNHEDGED_HOLD_S    position hold limit   (default: 30.0)
    ARBCORE_CB_COOLDOWN_S          breaker cooldown      (default: 60.0)

    # ── Phase 4: Oracle & Feed Integrity ──────────────────
    ARBCORE_USE_ORACLE             true | false          (default: false)
    ARBCORE_ORACLE_MAX_CI_PCT      max CI threshold      (default: 2.0)
    ARBCORE_ORACLE_REFRESH_S       refresh interval      (default: 2.0)
    ARBCORE_FEED_RECONCILE_S       reconcile interval    (default: 30.0)
    ARBCORE_FEED_DESYNC_PCT        max mid-price div     (default: 0.5)
    ARBCORE_FEE_REFRESH_S          fee cache TTL         (default: 300.0)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv


@dataclass(frozen=True)
class ExchangeCredentials:
    """Read-only container for a single exchange's API credentials."""

    api_key: str
    api_secret: str
    passphrase: str = ""  # only required by some exchanges (e.g. OKX, KuCoin)

    def __repr__(self) -> str:
        """Never leak secrets to logs."""
        return f"ExchangeCredentials(api_key=***masked***, api_secret=***masked***)"


@dataclass(frozen=True)
class RiskLimits:
    """Hard risk boundaries — immutable once loaded."""

    max_trade_usd: float = 100.0
    max_drawdown_pct: float = 2.0
    max_slippage_pct: float = 0.15
    max_open_exposure_usd: float = 500.0
    daily_loss_limit_usd: float = 50.0

    # ── Phase 4: Circuit Breaker thresholds ──────────────
    max_consecutive_api_failures: int = 3
    max_order_latency_ms: float = 5000.0
    max_ws_silence_s: float = 20.0
    max_unhedged_hold_s: float = 30.0
    circuit_breaker_cooldown_s: float = 60.0


@dataclass(frozen=True)
class Settings:
    """Top-level application configuration — frozen after construction."""

    mode: str  # "dry_run" | "live"
    enabled_exchanges: List[str]
    log_level: str
    poll_interval_s: float
    risk: RiskLimits
    credentials: Dict[str, ExchangeCredentials] = field(repr=False)
    strategy: str = "spatial"           # "spatial" | "triangular" | "both"
    use_websocket: bool = True
    concurrent_execution: bool = True
    health_port: int = 8080

    # ── Phase 4: Oracle & Feed Integrity ─────────────────
    use_oracle: bool = False
    oracle_max_ci_pct: float = 2.0
    oracle_refresh_interval_s: float = 2.0
    feed_reconcile_interval_s: float = 30.0
    feed_desync_threshold_pct: float = 0.5
    fee_refresh_interval_s: float = 300.0

    # ── Factory ──────────────────────────────────────────

    @classmethod
    def load(cls, env_path: str | Path | None = None) -> "Settings":
        """
        Build a Settings instance from environment variables.

        Parameters
        ----------
        env_path : optional path to a `.env` file for local development.
                   Defaults to `<project_root>/.env`.
        """
        if env_path is None:
            env_path = Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(dotenv_path=env_path, override=False)

        mode = os.getenv("ARBCORE_MODE", "dry_run").lower()
        if mode not in ("dry_run", "live"):
            raise ValueError(f"ARBCORE_MODE must be 'dry_run' or 'live', got '{mode}'")

        # Phase 4: Default to binance + kucoin (Tier-2 stale-quote targeting)
        enabled = [
            e.strip().lower()
            for e in os.getenv("ARBCORE_EXCHANGES", "binance,kucoin").split(",")
            if e.strip()
        ]

        risk = RiskLimits(
            max_trade_usd=float(os.getenv("ARBCORE_MAX_TRADE_USD", "100.0")),
            max_drawdown_pct=float(os.getenv("ARBCORE_MAX_DRAWDOWN_PCT", "2.0")),
            max_slippage_pct=float(os.getenv("ARBCORE_MAX_SLIPPAGE_PCT", "0.15")),
            max_open_exposure_usd=float(
                os.getenv("ARBCORE_MAX_OPEN_EXPOSURE_USD", "500.0")
            ),
            daily_loss_limit_usd=float(
                os.getenv("ARBCORE_DAILY_LOSS_LIMIT_USD", "50.0")
            ),
            # Phase 4: Circuit breaker thresholds
            max_consecutive_api_failures=int(
                os.getenv("ARBCORE_MAX_API_FAILURES", "3")
            ),
            max_order_latency_ms=float(
                os.getenv("ARBCORE_MAX_ORDER_LATENCY_MS", "5000.0")
            ),
            max_ws_silence_s=float(
                os.getenv("ARBCORE_MAX_WS_SILENCE_S", "20.0")
            ),
            max_unhedged_hold_s=float(
                os.getenv("ARBCORE_MAX_UNHEDGED_HOLD_S", "30.0")
            ),
            circuit_breaker_cooldown_s=float(
                os.getenv("ARBCORE_CB_COOLDOWN_S", "60.0")
            ),
        )

        credentials = cls._load_credentials(enabled)

        # Phase 4: Default to spatial (Tier-2 stale-quote targeting)
        strategy = os.getenv("ARBCORE_STRATEGY", "spatial").lower()
        if strategy not in ("spatial", "triangular", "both"):
            raise ValueError(
                f"ARBCORE_STRATEGY must be 'spatial', 'triangular', or 'both', got '{strategy}'"
            )

        return cls(
            mode=mode,
            enabled_exchanges=enabled,
            log_level=os.getenv("ARBCORE_LOG_LEVEL", "INFO").upper(),
            poll_interval_s=float(os.getenv("ARBCORE_POLL_INTERVAL", "1.0")),
            risk=risk,
            credentials=credentials,
            strategy=strategy,
            use_websocket=os.getenv("ARBCORE_USE_WEBSOCKET", "true").lower() == "true",
            concurrent_execution=os.getenv("ARBCORE_CONCURRENT_EXEC", "true").lower() == "true",
            health_port=int(os.getenv("ARBCORE_HEALTH_PORT", os.getenv("PORT", "8080"))),
            # Phase 4: Oracle & feed integrity
            use_oracle=os.getenv("ARBCORE_USE_ORACLE", "false").lower() == "true",
            oracle_max_ci_pct=float(os.getenv("ARBCORE_ORACLE_MAX_CI_PCT", "2.0")),
            oracle_refresh_interval_s=float(os.getenv("ARBCORE_ORACLE_REFRESH_S", "2.0")),
            feed_reconcile_interval_s=float(os.getenv("ARBCORE_FEED_RECONCILE_S", "30.0")),
            feed_desync_threshold_pct=float(os.getenv("ARBCORE_FEED_DESYNC_PCT", "0.5")),
            fee_refresh_interval_s=float(os.getenv("ARBCORE_FEE_REFRESH_S", "300.0")),
        )

    # ── Private helpers ──────────────────────────────────

    @staticmethod
    def _load_credentials(
        exchanges: List[str],
    ) -> Dict[str, ExchangeCredentials]:
        """
        Read API keys for each enabled exchange from the environment.
        In dry_run mode credentials may be empty strings; in live mode
        missing credentials raise immediately so we fail fast.
        """
        mapping: Dict[str, ExchangeCredentials] = {}
        for ex in exchanges:
            prefix = ex.upper()
            mapping[ex] = ExchangeCredentials(
                api_key=os.getenv(f"{prefix}_API_KEY", ""),
                api_secret=os.getenv(f"{prefix}_API_SECRET", ""),
                passphrase=os.getenv(f"{prefix}_PASSPHRASE", ""),
            )
        return mapping
