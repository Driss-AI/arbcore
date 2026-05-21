"""
Exchange Factory
================
Creates and connects ``CCXTAdapter`` instances for every exchange
enabled in the settings.  Returns them as a dict keyed by exchange name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict

from exchanges.ccxt_adapter import CCXTAdapter
from utils.logger import get_logger

if TYPE_CHECKING:
    from config.settings import Settings
    from exchanges.base import BaseExchangeAdapter

logger = get_logger(__name__)


async def create_adapters(
    settings: "Settings",
) -> Dict[str, "BaseExchangeAdapter"]:
    """
    Instantiate and connect one adapter per enabled exchange.

    Exchanges that fail to connect (e.g. geo-blocked) are skipped with a
    warning rather than aborting the whole process.  The engine will fail
    later if fewer than 2 adapters connect and no strategy can run.

    Parameters
    ----------
    settings : application configuration (includes credentials and mode).

    Returns
    -------
    Dict mapping exchange name → connected adapter (only successful ones).
    """
    adapters: Dict[str, "BaseExchangeAdapter"] = {}

    for exchange_id in settings.enabled_exchanges:
        creds = settings.credentials.get(exchange_id)
        if creds is None:
            logger.warning("No credentials block for '%s' — skipping.", exchange_id)
            continue

        adapter = CCXTAdapter(
            exchange_id=exchange_id,
            credentials=creds,
            sandbox=False,
        )
        try:
            await adapter.connect()
            adapters[exchange_id] = adapter
        except Exception as exc:
            logger.warning(
                "Failed to connect to %s (%s) — skipping. "
                "If this is Binance, Railway IPs are geo-blocked (HTTP 451); "
                "set ARBCORE_EXCHANGES to kucoin,gateio or similar.",
                exchange_id, exc,
            )

    if not adapters:
        raise RuntimeError(
            "No exchanges connected. Check ARBCORE_EXCHANGES and network access."
        )

    return adapters
