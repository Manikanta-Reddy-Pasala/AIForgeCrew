"""External docs index — Spring / React / MongoDB official docs.

KISS: filesystem-cached chunks, embedded with the existing embed
sidecar (BAAI/bge-m3 :8764). Lookup returns top-K chunks for a
``(library, query)`` pair.

Bootstrap workflow:
1. ``ingest(library, urls=[...])`` — fetch + chunk + embed + persist.
2. ``lookup_doc(library, query, top_k=3)`` — vector NN over the
   library's chunks.

Storage: SQLite at ``$AIFORGE_DOCS_DIR/<library>.db`` with two
tables: ``chunks(id, text, url, anchor)`` and
``embeddings(id, vec BLOB)``. KISS = SQLite + cosine in numpy; no
pgvector / Neo4j dependency.

Toggle via ``AIFORGE_DOCS_INDEX=0`` (default on).

Public surface:
- ``ingest(library, urls, chunk_chars=1500)``
- ``lookup_doc(library, query, top_k=3) -> list[dict]``
- ``list_libraries() -> list[str]``
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from pathlib import Path
from aiforge_core.config.paths import config_dir


def _root() -> Path:
    p = Path(os.environ.get(
        "AIFORGE_DOCS_DIR", os.path.join(os.path.expanduser(
            str(config_dir())), "docs"),
    ))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _db(library: str) -> sqlite3.Connection:
    db = _root() / f"{library}.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunks ("
        " id INTEGER PRIMARY KEY,"
        " text TEXT,"
        " url TEXT,"
        " anchor TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embeddings ("
        " id INTEGER PRIMARY KEY,"
        " vec BLOB)"
    )
    return conn


# ───────── ingest ──────────────────────────────────────────────────


def ingest(library: str, urls: list[str], *, chunk_chars: int = 1500) -> int:
    """Fetch each URL, chunk + embed + persist. Returns rows added."""
    if os.environ.get("AIFORGE_DOCS_INDEX", "1") != "1":
        return 0
    conn = _db(library)
    added = 0
    for url in urls:
        try:
            text = _fetch(url)
        except Exception as exc:
            print(f"[docs_index] fetch {url} failed: {exc}")
            continue
        for anchor, chunk in _chunk(text, chunk_chars=chunk_chars):
            try:
                vec = _embed(chunk)
            except Exception as exc:
                print(f"[docs_index] embed failed: {exc}")
                continue
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO chunks(text, url, anchor) VALUES(?,?,?)",
                (chunk, url, anchor),
            )
            row_id = cur.lastrowid
            cur.execute(
                "INSERT INTO embeddings(id, vec) VALUES(?,?)",
                (row_id, _vec_to_blob(vec)),
            )
            added += 1
        conn.commit()
    conn.close()
    return added


def list_libraries() -> list[str]:
    return sorted(p.stem for p in _root().glob("*.db"))


# ───────── query ───────────────────────────────────────────────────


def lookup_doc(library: str, query: str, *, top_k: int = 3) -> list[dict]:
    """Return top-K chunks. Empty list when library/query absent."""
    if not query.strip():
        return []
    db_path = _root() / f"{library}.db"
    if not db_path.exists():
        return []
    try:
        qvec = _embed(query)
    except Exception:
        return []
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT chunks.id, chunks.text, chunks.url, chunks.anchor, embeddings.vec "
        "FROM chunks JOIN embeddings ON chunks.id = embeddings.id"
    ).fetchall()
    conn.close()
    if not rows:
        return []
    scored: list[tuple[float, dict]] = []
    for rid, text, url, anchor, vec_blob in rows:
        try:
            sim = _cosine(qvec, _blob_to_vec(vec_blob))
        except Exception:
            continue
        scored.append((sim, {
            "id": rid, "text": text, "url": url, "anchor": anchor,
            "score": round(sim, 4),
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:top_k]]


# ───────── helpers ────────────────────────────────────────────────


def _fetch(url: str) -> str:
    # Egress + SSRF. This had neither, while taking an arbitrary URL from the
    # `aiforge-maint docs ingest` CLI — the widest ungated fetcher in the tree.
    from aiforge_core.net import egress as _egress
    _ref = _egress.check(url)
    if _ref is not None:
        raise OSError(f"docs fetch refused: {_ref.get('error')} — "
                      f"{_ref.get('hint', '')}")
    from aiforge_core.net.ssl import SSRFBlocked, guard_public_url
    try:
        guard_public_url(url)
    except SSRFBlocked as exc:
        if exc.kind != "dns":
            raise OSError(f"docs fetch blocked (ssrf): {exc}") from exc
    req = urllib.request.Request(url, headers={
        "User-Agent": "aiforge-docs-index/0.1",
    })
    # Public/arbitrary doc URL — keep stdlib default TLS verification
    # (the AIFORGE_LLM_SSL_VERIFY opt-out is scoped to internal hosts).
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read(2_000_000).decode("utf-8", "replace")


def _chunk(text: str, *, chunk_chars: int) -> list[tuple[str, str]]:
    """Split into ``(anchor, body)`` pairs. KISS: split on blank
    lines, then re-pack to roughly ``chunk_chars`` size each."""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    cur_len = 0
    for sentence in parts:
        if cur_len + len(sentence) > chunk_chars and buf:
            out.append(("", " ".join(buf)))
            buf = []
            cur_len = 0
        buf.append(sentence)
        cur_len += len(sentence)
    if buf:
        out.append(("", " ".join(buf)))
    return out


def _embed(text: str) -> list[float]:
    """Call the embed sidecar — the same one ``aiforge_core.memory.embed``
    uses for memory facts. KISS: shell out via internal helper to
    avoid pulling sentence-transformers into this module."""
    from aiforge_core.memory.embed import embed
    return list(embed(text[:8000]))


def _vec_to_blob(vec) -> bytes:
    import struct
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    import struct
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a, b) -> float:
    import math
    if len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)
