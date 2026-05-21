"""
Logging Configuration
=====================
Provides a single ``get_logger(name)`` factory used by every module.

- **Local / dev:**  human-readable, coloured console output.
- **Production (Railway):**  structured JSON lines for log aggregation.

The format is selected automatically based on the ``ARBCORE_LOG_FORMAT``
environment variable (``text`` | ``json``; default ``text``).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    """Minimal structured JSON formatter — no external dependencies."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    """
    Return a configured logger for *name*.

    Idempotent — calling twice with the same name returns the same logger.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # already configured

    level = os.getenv("ARBCORE_LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    fmt = os.getenv("ARBCORE_LOG_FORMAT", "text").lower()

    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    logger.addHandler(handler)
    logger.propagate = False
    return logger
