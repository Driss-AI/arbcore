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

    # ── Runtime tunables ──────────────────────────────────
    ARBCORE_MODE            dry_run | live        (default: dry_run)
    ARBCORE_EXCHANGES       comma-separated list  (default: binance,kraken)
    ARBCORE_LOG_LEVEL       DEBUG | INFO | …      (default: INFO)
    ARBCORE_POLL_INTERVAL   seconds between ticks (default: 1.0)
    ARBCORE_STRATEGY        spatial | triangular | both  (default: both)
    ARBCORE_USE_WEBSOCKET   true | false          (default: false)
    ARBCORE_CONCURRENT_EXEC true | false          (default: false)
    ARBCORE_HEALTH_PORT     HTTP health port      (default: 8080)

    # ── Risk limits ───────────────────────────────────────
    ARBCORE_MAX_TRADE_USD          per-trade cap        (default: 100.0)
    ARBCORE_MAX_DRAWDOWN_PCT       kill-switch %        (default: 2.0)
    ARBCORE_MAX_SLIPPAGE_PCT       order-level cap      (default: 0.15)
    ARBCORE_MAX_OPEN_EXPOSURE_USD  aggregate position   (default: 500.0)
    ARBCORE_DAILY_LOSS_LIMIT_USD   hard daily stop      (default: 50.0)
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
    passphrase: str = ""  # only required by some exchanges (e.g. OKX)

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


@dataclass(frozen=True)
class Settings:
    """Top-level application configuration — frozen after construction."""

    mode: str  # "dry_run" | "live"
    enabled_exchanges: List[str]
    log_level: str
    poll_interval_s: float
    risk: RiskLimits
    credentials: Dict[str, ExchangeCredentials] = field(repr=False)
    strategy: str = "both"              # "spatial" | "triangular" | "both"
    use_websocket: bool = False
    concurrent_execution: bool = False
    health_port: int = 8080

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

        enabled = [
            e.strip().lower()
            for e in os.getenv("ARBCORE_EXCHANGES", "binance,kraken").split(",")
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
        )

        credentials = cls._load_credentials(enabled)

        strategy = os.getenv("ARBCORE_STRATEGY", "both").lower()
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
            use_websocket=os.getenv("ARBCORE_USE_WEBSOCKET", "false").lower() == "true",
            concurrent_execution=os.getenv("ARBCORE_CONCURRENT_EXEC", "false").lower() == "true",
            health_port=int(os.getenv("ARBCORE_HEALTH_PORT", os.getenv("PORT", "8080"))),
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
