"""
Exchange Factory
================
Creates and connects ``CCXTAdapter`` instances for every exchange
enabled in the settings.  Returns them as a dict keyed by exchange name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict

from exchanges.ccxt_adapter import CCXTAdapter

if TYPE_CHECKING:
    from config.settings import Settings
    from exchanges.base import BaseExchangeAdapter


async def create_adapters(
    settings: "Settings",
) -> Dict[str, "BaseExchangeAdapter"]:
    """
    Instantiate and connect one adapter per enabled exchange.

    Parameters
    ----------
    settings : application configuration (includes credentials and mode).

    Returns
    -------
    Dict mapping exchange name → connected adapter.
    """
    sandbox = False
    adapters: Dict[str, "BaseExchangeAdapter"] = {}

    for exchange_id in settings.enabled_exchanges:
        creds = settings.credentials.get(exchange_id)
        if creds is None:
            raise ValueError(
                f"No credentials block found for exchange '{exchange_id}'"
            )

        adapter = CCXTAdapter(
            exchange_id=exchange_id,
            credentials=creds,
            sandbox=sandbox,
        )
        await adapter.connect()
        adapters[exchange_id] = adapter

    return adapters
