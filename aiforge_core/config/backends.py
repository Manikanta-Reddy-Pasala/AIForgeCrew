"""Resolve and announce the data backends.

This is a SQLite-only build: every data store (tickets, memory, chat, jobs)
is embedded SQLite. These helpers are kept as a stable seam — ``resolve_backends``
still reports the per-store map (for the boot log), and ``require_data_backends``
is a no-op (there is no external backend to require).

``boot_log()`` is soft (a logging failure must never crash boot).
"""
from __future__ import annotations

import logging

log = logging.getLogger("aiforge.boot")


def resolve_backends() -> dict[str, str]:
    """Map each data store to the backend name it uses. SQLite-only build."""
    return {
        "tickets": "sqlite",
        "memory": "sqlite",
        "chat": "sqlite",
        "jobs": "sqlite",
    }


def boot_log() -> None:
    """Emit ONE INFO line naming every data backend. Never raises."""
    try:
        b = resolve_backends()
        log.info(
            "aiforge.boot: tickets=%s memory=%s chat=%s jobs=%s",
            b["tickets"], b["memory"], b["chat"], b["jobs"],
        )
    except Exception:  # noqa: BLE001 — a logging failure must not crash boot
        pass


def require_data_backends() -> None:
    """No-op — this is a SQLite-only build, so there is no external data
    backend to require. Kept as a stable boot-time call site."""
    return
