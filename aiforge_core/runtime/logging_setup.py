"""Structured JSON logging for the orchestrator.

One JSON object per line. Stdout → parseable by `jq`. File handler per role
writes to `~/.aiforge/logs/orchestrator-<role>.ndjson`, rotated daily by
external `logrotate` or `newsyslog` — this module only opens the file.

Usage:
    from aiforge_core.runtime.logging_setup import get_logger, emit
    log = get_logger("sr_developer", ticket="ONE-70")
    log.info("tick.start")
    emit(log, "llm.turn", turn=3, tool="search", dur_ms=412)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from logging import Logger
from pathlib import Path

from .config import LOG_DIR


_DEFAULT_FIELDS = ("ts", "level", "role", "ticket", "event")


class _JsonFormatter(logging.Formatter):
    """Minimal structured formatter — dumps record.__dict__['aiforge'] as the body."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int((record.created % 1) * 1000):03d}Z",
            "level": record.levelname.lower(),
        }
        # structured fields passed via extra={"aiforge": {...}}
        extra = getattr(record, "aiforge", None) or {}
        payload.update(extra)
        # message comes last so it always appears
        payload["event"] = record.getMessage()
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_CONFIGURED = False


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("aiforge")
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fmt = _JsonFormatter()
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    root.propagate = False
    _CONFIGURED = True


def get_logger(role: str, *, ticket: str | None = None) -> Logger:
    """Return a logger scoped to a role (+ optional ticket). Attaches a file
    handler under `~/.aiforge/logs/orchestrator-<role>.ndjson`."""
    _configure_root()
    logger = logging.getLogger(f"aiforge.{role}")
    if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        fh = logging.FileHandler(
            os.path.join(LOG_DIR, f"orchestrator-{role}.ndjson"),
            encoding="utf-8",
        )
        fh.setFormatter(_JsonFormatter())
        logger.addHandler(fh)
    # Stash static context so emit() / direct log calls can pick it up.
    logger._aiforge_role = role
    logger._aiforge_ticket = ticket
    return logger


def emit(log: Logger, event: str, **fields) -> None:
    """Emit a structured event on an already-configured role logger."""
    ctx = {
        "role": getattr(log, "_aiforge_role", None),
        "ticket": getattr(log, "_aiforge_ticket", None),
    }
    ctx.update(fields)
    log.info(event, extra={"aiforge": ctx})
