"""Resolve, announce, and (optionally) HARD-GUARD the data backends.

Four data stores back the running system:

  * tickets  -> Postgres when ``AIFORGE_PG_URL`` is set, else SQLite
  * memory   -> neo4j / postgres / sqlite (see ``memory.backend_select``)
  * chat     -> Postgres when ``AIFORGE_PG_URL`` is set, else SQLite
  * jobs     -> Postgres when ``AIFORGE_PG_URL`` is set, else SQLite

In the zero-infra / ``--lite`` deploy every store is embedded SQLite and that
is fine. In the docker / hybrid ("data-driven") deploy the run script exports
``AIFORGE_REQUIRE_DATA_BACKEND=1``; if ANY store then still resolves to an
embedded SQLite backend we abort boot LOUDLY rather than silently scribbling
``.db`` files next to a real Postgres+Neo4j stack.

``boot_log()`` is soft (a logging failure must never crash boot).
``require_data_backends()`` is intentionally hard-fail.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.boot")

# Backend names that mean "embedded, single-file, no external service" — the
# ones that are NOT allowed once data-driven mode is required.
_EMBEDDED = {"sqlite", "embedded"}


def _pg_target() -> str:
    """host:port of the configured Postgres, for the boot log. Best-effort."""
    from aiforge_core.config import env as _env
    url = getattr(_env, "AIFORGE_PG_URL", None)
    if not url:
        return ""
    try:
        # postgresql://user:pass@host:port/db  -> host:port
        after = str(url).split("://", 1)[-1]
        hostpart = after.split("@")[-1].split("/")[0]
        return hostpart
    except Exception:  # noqa: BLE001
        return ""


def resolve_backends() -> dict[str, str]:
    """Map each data store to the backend name it will use THIS process.

    Reads ``config.env`` attributes (so tests can monkeypatch them) plus the
    live memory-backend resolver.
    """
    from aiforge_core.config import env as _env
    from aiforge_core.memory.backend_select import memory_backend

    use_sqlite = bool(getattr(_env, "AIFORGE_USE_SQLITE", True))
    tabular = "sqlite" if use_sqlite else "postgres"
    return {
        "tickets": tabular,
        "memory": memory_backend(),
        "chat": tabular,
        "jobs": tabular,
    }


def _label(backend: str, pg_target: str) -> str:
    if backend == "postgres" and pg_target:
        return f"postgres@{pg_target}"
    return backend


def boot_log() -> None:
    """Emit ONE INFO line naming every data backend. Never raises."""
    try:
        b = resolve_backends()
        tgt = _pg_target()
        log.info(
            "aiforge.boot: tickets=%s memory=%s chat=%s jobs=%s",
            _label(b["tickets"], tgt),
            b["memory"],
            _label(b["chat"], tgt),
            _label(b["jobs"], tgt),
        )
    except Exception:  # noqa: BLE001 — a logging failure must not crash boot
        pass


def require_data_backends() -> None:
    """When ``AIFORGE_REQUIRE_DATA_BACKEND=1`` and any store is still embedded
    SQLite, RAISE — so a misconfigured data-driven deploy fails loud instead of
    silently writing SQLite. No-op when the flag is unset (``--lite`` path)."""
    if os.environ.get("AIFORGE_REQUIRE_DATA_BACKEND") != "1":
        return
    b = resolve_backends()
    weak = sorted(k for k, v in b.items() if v in _EMBEDDED)
    if not weak:
        return
    raise RuntimeError(
        "data-driven mode requires Postgres+Neo4j but these stores are still "
        f"embedded SQLite: {', '.join(weak)}. Set AIFORGE_PG_URL and "
        "AIFORGE_MEMORY_BACKEND=neo4j, or use ./run.sh --lite. "
        f"(resolved: tickets={b['tickets']} memory={b['memory']} "
        f"chat={b['chat']} jobs={b['jobs']})"
    )
