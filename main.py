"""
ArbCore — Deterministic Crypto Arbitrage Execution Engine
==========================================================
Entry point for the 24/7 background worker.

This module bootstraps configuration, initialises exchange connections,
and starts the main event loop.  No trading logic lives here — it only
orchestrates the lifecycle defined in `core/engine.py`.

Usage (local):
    python main.py

Usage (Railway / production):
    Procfile → `worker: python main.py`
"""

from __future__ import annotations

import asyncio
import signal
import sys

from config.settings import Settings
from core.engine import Engine
from utils.logger import get_logger

logger = get_logger(__name__)


def _handle_shutdown(engine: Engine) -> None:
    """Register OS-level signal handlers for graceful shutdown."""

    def _shutdown(sig: signal.Signals, _frame) -> None:  # type: ignore[override]
        logger.warning("Received %s — initiating graceful shutdown …", sig.name)
        engine.request_shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)


async def _run() -> None:
    settings = Settings.load()
    logger.info(
        "ArbCore v0.1.0 starting | mode=%s | exchanges=%s",
        settings.mode,
        settings.enabled_exchanges,
    )

    engine = Engine(settings)
    _handle_shutdown(engine)

    try:
        await engine.start()
    except Exception:
        logger.exception("Fatal unhandled exception in engine loop")
        sys.exit(1)
    finally:
        await engine.shutdown()
        logger.info("ArbCore shut down cleanly.")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
