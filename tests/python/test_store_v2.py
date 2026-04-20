"""Store v2 tests. Uses ephemeral Postgres via AIFORGE_PGMEM_DSN env."""
from __future__ import annotations
import os
import pytest
import psycopg

DSN = os.environ.get("AIFORGE_PGMEM_DSN",
                     "host=127.0.0.1 port=5432 dbname=aiforge")


def _pg_available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(),
                                 reason="aiforge DB not reachable; run install-pg-aiforge.sh")


@pytest.fixture
def store():
    from aiforge_core.store_v2 import Store
    s = Store(dsn=DSN)
    s.ensure_schema()
    # start clean
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("TRUNCATE memories RESTART IDENTITY")
        cur.execute("TRUNCATE memory_proposals RESTART IDENTITY")
    return s


def test_ensure_schema_idempotent(store):
    store.ensure_schema()
    store.ensure_schema()  # no error
