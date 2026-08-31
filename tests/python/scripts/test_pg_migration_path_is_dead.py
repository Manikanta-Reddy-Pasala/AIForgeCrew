"""The Postgres→SQLite upgrade path cannot work, and this pins that fact.

deploy/converge runs `scripts/migrate_to_sqlite.py` as a subprocess when it
finds an old `aiforge-postgres` container, and only tears the container down
if that exits 0. The script's first import is `_PgChatStore`, which was
removed with Postgres support — so it exits 1, converge returns
{"ok": False, "step": "postgres_to_sqlite"} and keeps Docker, forever.

For anyone upgrading from an old dockerized install that means their chat and
tickets are never migrated and every boot retries the same failure.

This is NOT a fix — fixing it needs a decision: either restore a Postgres read
path (which needs a psycopg dependency this build deliberately does not ship)
or drop the migration and say so. These tests record the exact state so the
decision is made on facts, and so whichever way it goes, this file is the
thing that has to be updated.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]


def test_the_migration_script_cannot_import():
    """Its very first import is gone."""
    r = subprocess.run(
        [sys.executable, "scripts/migrate_to_sqlite.py"],
        cwd=str(_REPO), capture_output=True, text=True, timeout=180,
        env=dict(os.environ, AIFORGE_PG_URL="postgresql://u@127.0.0.1/db",
                 AIFORGE_MODE="lite"))
    assert r.returncode != 0
    assert "_PgChatStore" in (r.stderr + r.stdout), \
        "if this changed, the migration path may be alive again — re-check converge"


def test_the_postgres_chat_store_really_is_gone():
    from aiforge_core.runtime import chat_store
    assert not hasattr(chat_store, "_PgChatStore")
    assert hasattr(chat_store, "_SqliteChatStore"), "the SQLite side must remain"


def test_converge_reports_failure_rather_than_deleting_the_source():
    """The one saving grace: because the migration reports failure, converge
    does NOT remove the Postgres container. The data is stuck, not lost."""
    from aiforge_core.deploy import converge as cv
    import inspect
    src = inspect.getsource(cv.converge)
    assert 'return {"ok": False, "step": "postgres_to_sqlite"}' in src

    # There are TWO _remove_db_infra() calls: one in the already-migrated
    # branch (legitimately BEFORE the migration code) and one after a
    # successful migration. The invariant is about the second: within the
    # migration flow, teardown must follow the migration guard.
    migrate_at = src.index("if not _migrate_pg_to_sqlite()")
    teardown_after = src.find("_remove_db_infra()", migrate_at)
    assert teardown_after > migrate_at, \
        "the teardown must stay AFTER the migration guard, never before it"
