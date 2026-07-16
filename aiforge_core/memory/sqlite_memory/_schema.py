"""Schema, connection + embedding-safety core for the SQLite memory store.

Leaf layer: DDL constants, the process lock, the sqlite-vec ANN setup, the
db-path resolver and the ``_conn`` context manager. Everything else in the
package layers on top of this.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from aiforge_core.memory import local_embed

_log = logging.getLogger("aiforge.memory")
_EMBED_WARNED = False


def _safe_embed(text: str) -> list:
    """Embed ``text``, or return the ``[]`` sentinel if the model can't load.

    A memory WRITE must never be lost because the embedder is unavailable — the
    note is stored and stays findable via keyword/FTS, and ``reembed_all`` fills
    the missing vector once the model is back. (The recall path stays loud — a
    broken semantic search still raises, per the no-silent-degrade rule.)"""
    global _EMBED_WARNED
    try:
        return local_embed.embed(text)
    except Exception as exc:                      # noqa: BLE001 — degrade, don't lose
        if not _EMBED_WARNED:
            _log.warning(
                "embedder unavailable (%s); storing notes WITHOUT vectors — "
                "run `aiforge-maint memory reembed` (or restart with the model "
                "present) to backfill semantic recall", exc)
            _EMBED_WARNED = True
        return []

_LOCK = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS memory_units (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL DEFAULT 'observation',
    wing        TEXT,
    source      TEXT,
    title       TEXT,
    text        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    metadata    TEXT NOT NULL DEFAULT '{}',
    repo        TEXT,
    ticket      TEXT,
    embedding   TEXT NOT NULL DEFAULT '[]',
    event_time  REAL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS memory_units_repo ON memory_units(repo);
CREATE INDEX IF NOT EXISTS memory_units_kind ON memory_units(kind);
CREATE INDEX IF NOT EXISTS memory_units_ticket ON memory_units(ticket);
CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Keyword (BM25) search — an FTS5 external-content index over memory_units.text,
# kept in sync by triggers. Gives exact-token / prefix recall (ticket ids,
# service names, hashes) that embeddings blur, fused with vector recall. Wrapped
# so a build without FTS5 degrades to vector-only instead of crashing.
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    text, content='memory_units', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS memory_units_ai AFTER INSERT ON memory_units BEGIN
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memory_units_ad AFTER DELETE ON memory_units BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memory_units_au AFTER UPDATE ON memory_units BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


# sqlite-vec ANN index — the production vector path (AIFORGE_EMBED_BACKEND=
# semantic). vec0 virtual table mirrors memory_units.embedding via triggers;
# recall does a real KNN instead of an O(N) Python cosine scan. NO fallback:
# when the semantic backend is selected the extension is REQUIRED (recall raises
# if it's missing) — the Python-cosine path below runs only under the dev/test
# 'hash' backend.
_VEC_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS vec_memory_ai AFTER INSERT ON memory_units BEGIN
    INSERT INTO vec_memory(rowid, embedding) VALUES (new.id, new.embedding);
END;
CREATE TRIGGER IF NOT EXISTS vec_memory_ad AFTER DELETE ON memory_units BEGIN
    DELETE FROM vec_memory WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS vec_memory_au AFTER UPDATE ON memory_units BEGIN
    DELETE FROM vec_memory WHERE rowid = old.id;
    INSERT INTO vec_memory(rowid, embedding) VALUES (new.id, new.embedding);
END;
"""


def _vec_enabled() -> bool:
    # every REAL vector backend uses the sqlite-vec ANN index (fast KNN); only
    # the lexical hash backend uses the brute-force scan. sqlite-vec ships with
    # the embed-static extra.
    return os.environ.get("AIFORGE_EMBED_BACKEND", "hash").strip().lower() in (
        "model2vec", "static", "api", "openai", "lmstudio", "ollama")


def _init_vec(c) -> None:
    """Load sqlite-vec + create the vec0 table (dim = active embedder) + sync
    triggers, backfilling existing rows. Raises so a broken semantic setup is
    LOUD (no silent cosine fallback)."""
    import sqlite_vec
    from aiforge_core.memory import local_embed
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    dim = int(local_embed.embed_dim())
    # If a vec_memory table exists at a DIFFERENT dimension (backend/model
    # switched, or a migration imported rows from another embedder), DROP it so
    # it's recreated at the active dim — else every insert would dim-mismatch and
    # KNN would silently return nothing.
    existing = c.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'vec_memory'").fetchone()
    if existing and existing[0] and f"float[{dim}]" not in existing[0]:
        c.execute("DROP TABLE vec_memory")
    c.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory USING "
        f"vec0(embedding float[{dim}] distance_metric=cosine)")
    c.executescript(_VEC_TRIGGERS)
    vn = c.execute("SELECT count(*) FROM vec_memory").fetchone()[0]
    un = c.execute("SELECT count(*) FROM memory_units").fetchone()[0]
    if un and vn < un:
        # INCREMENTAL backfill — insert only rows MISSING from the vec index, no
        # destructive DELETE-all rebuild. So concurrent readers (which enter
        # _conn without _LOCK) don't race on a table-wide delete, and a row that
        # can't be inserted doesn't force a full re-scan every connection — the
        # NOT IN gap just shrinks to it and skips otherwise.
        for r in c.execute(
                "SELECT id, embedding FROM memory_units "
                "WHERE id NOT IN (SELECT rowid FROM vec_memory)").fetchall():
            try:
                c.execute("INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
                          (r["id"], r["embedding"]))
            except sqlite3.OperationalError:
                continue                   # unbackfillable row → leave it out


def _db_path() -> str:
    return os.environ.get(
        "AIFORGE_MEMORY_DB_PATH",
        os.path.join(
            os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")),
            "memory.db",
        ),
    )


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite3.connect(path, timeout=30.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.executescript(_DDL)
        try:
            c.executescript(_FTS_DDL)          # FTS5 may be unavailable → soft
        except sqlite3.OperationalError:
            pass
        if _vec_enabled():                     # sqlite-vec ANN (semantic backend)
            _init_vec(c)                       # RAISES if the extension is missing
        yield c
        c.commit()
    finally:
        c.close()
