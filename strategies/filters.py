"""
Opportunity Filters — Oracle CI & Feed Health Gates
====================================================
Post-scan filters that validate opportunities against external signals
before they reach the risk manager.

From the research report:
    "If a Tier-2 exchange's price deviates from the Pyth composite
    price within the bounds of the CI, the algorithm ignores it as
    standard market noise. However, if the CEX price deviates outside
    the established CI, it acts as mathematical confirmation of a true
    latency window or a stale quote."

Filters are pure functions — no I/O, no side effects.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, List, Optional, Set

from utils.logger import get_logger

if TYPE_CHECKING:
    from data.oracle import OracleClient
    from strategies.base import Opportunity

logger = get_logger(__name__)


def filter_by_oracle_ci(
    opportunities: List["Opportunity"],
    oracle: Optional["OracleClient"],
) -> List["Opportunity"]:
    """
    Reject opportunities where the price divergence is within
    the oracle's Confidence Interval (i.e. just market noise).

    Only applies to spatial (cross-exchange) opportunities.
    Triangular arb (single-exchange) passes through unfiltered.
    """
    if oracle is None:
        return opportunities

    filtered: List["Opportunity"] = []

    for opp in opportunities:
        if opp.strategy != "spatial":
            filtered.append(opp)
            continue

        symbol = opp.legs[0].symbol if opp.legs else None
        if not symbol:
            continue

        buy_leg = next((l for l in opp.legs if l.side == "buy"), None)
        if buy_leg is None:
            continue

        result = oracle.is_divergence_significant(symbol, buy_leg.price)

        if result is True:
            logger.debug(
                "ORACLE_CONFIRM | %s | buy_price=%.2f — outside CI",
                symbol, buy_leg.price,
            )
            filtered.append(opp)
        elif result is False:
            logger.debug(
                "ORACLE_REJECT | %s | buy_price=%.2f — within CI (noise)",
                symbol, buy_leg.price,
            )
        else:
            # No oracle data — fail-open for liveness
            filtered.append(opp)

    return filtered


def filter_by_feed_health(
    opportunities: List["Opportunity"],
    desynced_feeds: Set[str],
) -> List["Opportunity"]:
    """
    Reject opportunities that involve a desynced exchange/symbol feed.
    """
    if not desynced_feeds:
        return opportunities

    filtered: List["Opportunity"] = []

    for opp in opportunities:
        legs_healthy = True
        for leg in opp.legs:
            key = f"{leg.exchange}/{leg.symbol}"
            if key in desynced_feeds:
                logger.debug(
                    "FEED_FILTER | rejecting %s — %s is desynced",
                    opp.strategy, key,
                )
                legs_healthy = False
                break

        if legs_healthy:
            filtered.append(opp)

    return filtered


def filter_by_book_freshness(
    opportunities: List["Opportunity"],
    max_age_ms: int = 5000,
) -> List["Opportunity"]:
    """Reject opportunities with stale order-book timestamps."""
    now_ms = int(time.time() * 1000)
    filtered: List["Opportunity"] = []

    for opp in opportunities:
        meta = opp.metadata or {}
        book_ts = meta.get("book_timestamp_ms")
        if book_ts and (now_ms - book_ts) > max_age_ms:
            logger.debug(
                "STALE_BOOK | rejecting %s — book age %dms",
                opp.strategy, now_ms - book_ts,
            )
            continue
        filtered.append(opp)

    return filtered
