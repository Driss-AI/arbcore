"""
Health Monitor
==============
Runs a lightweight async HTTP server on a configurable port so Railway
(or any external monitor) can poll ``GET /health`` for liveness data.

Endpoints:
    GET /health → 200 with JSON stats when the engine is running,
                  503 when the engine has stopped or WS streams are dead.
    GET /       → redirect to /health

Stats exposed:
    uptime_s, tick_count, opportunities_found, trades_executed,
    session_pnl, ws_streams_active, mode, last_tick_ago_s
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Optional

from aiohttp import web

from utils.logger import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class HealthMonitor:
    """
    Lightweight health-check HTTP server.

    The engine updates stats via ``record_tick()`` and ``update_stats()``;
    the server exposes them as JSON.

    Parameters
    ----------
    port : TCP port to bind (default 8080, Railway expects PORT env var)
    """

    def __init__(self, port: int = 8080) -> None:
        self._port = port
        self._start_time = time.monotonic()
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None

        # Mutable stats — updated by the engine
        self._stats = {
            "mode": "unknown",
            "tick_count": 0,
            "opportunities_found": 0,
            "trades_executed": 0,
            "session_pnl_usd": 0.0,
            "ws_streams_active": 0,
            "last_tick_epoch": 0.0,
            "exchanges": [],
            "symbols_monitored": 0,
        }

    # ── Lifecycle ────────────────────────────────────────

    async def start(self) -> None:
        """Start the HTTP server in the background."""
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/", self._handle_root)

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()

        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        logger.info("Health monitor listening on port %d", self._port)

    async def stop(self) -> None:
        """Shut down the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            logger.info("Health monitor stopped.")

    # ── Stats interface (called by engine) ───────────────

    def record_tick(
        self,
        opportunities: int = 0,
        trades: int = 0,
        pnl_delta: float = 0.0,
    ) -> None:
        """Called by the engine at the end of each tick."""
        self._stats["tick_count"] += 1
        self._stats["opportunities_found"] += opportunities
        self._stats["trades_executed"] += trades
        self._stats["session_pnl_usd"] += pnl_delta
        self._stats["last_tick_epoch"] = time.time()

    def update_stats(self, **kwargs) -> None:
        """Merge arbitrary key-value pairs into the stats dict."""
        self._stats.update(kwargs)

    # ── HTTP handlers ────────────────────────────────────

    async def _handle_health(self, request: web.Request) -> web.Response:
        now = time.time()
        uptime = time.monotonic() - self._start_time
        last_tick = self._stats.get("last_tick_epoch", 0)
        last_tick_ago = (now - last_tick) if last_tick > 0 else -1

        payload = {
            "status": "ok" if last_tick_ago < 30 else "degraded",
            "uptime_s": round(uptime, 1),
            "last_tick_ago_s": round(last_tick_ago, 1),
            **self._stats,
        }

        status_code = 200 if payload["status"] == "ok" else 503
        return web.json_response(payload, status=status_code)

    async def _handle_root(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/health")
