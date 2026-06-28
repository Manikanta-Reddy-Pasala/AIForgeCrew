"""Generic text → AFM ``Doc_v2`` + ``Note_v2`` ingest.

The implementation is intentionally minimal so the v1 spine can ship
in one commit. It does three things:

1. Resolve the source — local file path, ``http(s)://`` URL, or raw
   text passed inline.
2. Chunk the resolved text on paragraph boundaries with a soft size
   target (default 1200 chars). One chunk = one ``Note_v2``.
3. Write a single ``Doc_v2`` node as the parent (for "show me what's
   ingested from $url") and link each chunk's note via tags. We
   don't add ``MENTIONS`` edges here — Doc_v2 only carries URI +
   title, the chunks carry text.

What's deliberately out of scope (deferred to per-connector PRs):

* Confluence / Slack / Jira / Notion auth + API clients
* HTML → markdown conversion (Trafilatura, Readability)
* MIME sniffing beyond a tiny suffix table
* Embedding here (caller can pass an ``embed_fn``)

Public surface:

* ``ingest_external_source(driver, *, source, repo, source_type=...)``
  → ``{ok, doc_id, note_ids: list[str], errors: list[str]}``
"""
from __future__ import annotations

import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from uuid import uuid4

log = logging.getLogger("aiforge_memory.external_ingest")


_CHUNK_TARGET = int(os.environ.get("AIFORGE_INGEST_CHUNK_CHARS", "1200"))
_HTTP_TIMEOUT = float(os.environ.get("AIFORGE_INGEST_HTTP_TIMEOUT", "15"))


def _resolve_source(source: str) -> tuple[str, str, str | None]:
    """Resolve ``source`` to ``(text, kind, uri)``.

    ``kind`` is one of ``file`` | ``http`` | ``raw``. ``uri`` is the
    canonical reference string we write onto Doc_v2 (or ``None`` for
    raw inline text)."""
    if source.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(source, timeout=_HTTP_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
            return body, "http", source
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"http_fetch_failed: {exc}") from exc
    p = Path(source).expanduser()
    if p.is_file():
        return p.read_text(encoding="utf-8", errors="replace"), "file", str(p)
    # Treat as inline raw text.
    return source, "raw", None


def _chunk(text: str, *, target: int = _CHUNK_TARGET) -> list[str]:
    """Split on blank-line boundaries, then merge small pieces up to
    ``target`` chars. Very small inputs (≤ target) return one chunk."""
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if not current:
            current = para
            continue
        if len(current) + 2 + len(para) <= target:
            current = f"{current}\n\n{para}"
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


def ingest_external_source(
    driver,
    *,
    source: str,
    repo: str,
    source_type: str = "external",
    title: str | None = None,
    tags: list[str] | None = None,
    embed_fn: Callable[[str], list[float]] | None = None,
) -> dict:
    """Ingest ``source`` into AFM as one ``Doc_v2`` + N ``Note_v2``.

    Args:
        driver: live Neo4j driver.
        source: local file path, ``http(s)://`` URL, or raw text.
        repo: target AFM repo name. ``Repo`` node must already exist.
        source_type: free-form label (``confluence``, ``slack``,
            ``jira``, ``manual``, ``external``). Becomes a tag on the
            Doc + every Note.
        title: optional human title; defaults to the URI or
            ``"external:<source_type>"``.
        tags: extra tags applied to every node we write.
        embed_fn: optional callable that returns a 1024-d embedding
            for a chunk. When supplied the embedding is NOT stored
            here (the simpler ``Note_v2`` upsert has no embed slot);
            callers that want vector-recall over ingested chunks
            should write ``Observation_v2`` nodes instead — see the
            companion PR roadmap.

    Returns ``{ok, doc_id, note_ids, errors}``. Never raises into the
    caller; any failure surfaces in ``errors``.
    """
    out: dict = {"ok": False, "doc_id": None, "note_ids": [], "errors": []}

    try:
        text, kind, uri = _resolve_source(source)
    except RuntimeError as exc:
        out["errors"].append(str(exc))
        return out
    chunks = _chunk(text)
    if not chunks:
        out["errors"].append("empty_source")
        return out

    try:
        from aiforge_memory.features.memory.store import (
            upsert_doc, upsert_note,
        )
    except ImportError as exc:
        out["errors"].append(f"memory_store_unavailable: {exc}")
        return out

    base_tags = list(tags or [])
    base_tags.extend([f"source_type:{source_type}", f"ingest_kind:{kind}"])
    if uri:
        base_tags.append(f"uri:{uri}")

    final_title = title or uri or f"external:{source_type}"

    try:
        doc = upsert_doc(
            driver, repo=repo,
            url=uri or f"raw:{uuid4().hex[:8]}",
            title=final_title,
            summary=(text[:280] + ("…" if len(text) > 280 else "")),
            tags=base_tags,
        )
        out["doc_id"] = doc.get("id")
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"upsert_doc_failed: {exc}")
        # Still try to write notes — the Doc is a convenience, not a
        # hard parent.

    for idx, chunk_text in enumerate(chunks):
        try:
            note = upsert_note(
                driver, repo=repo,
                title=f"{final_title} #{idx + 1}",
                body=chunk_text,
                author="external_ingest",
                tags=base_tags + [f"chunk_idx:{idx}",
                                  f"doc_id:{out['doc_id'] or '-'}"],
            )
            out["note_ids"].append(note.get("id"))
            if embed_fn is not None:
                # Caller asked for embeddings but we have nowhere to
                # store them on Note_v2 today. Surface the gap.
                _ = embed_fn(chunk_text)
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"upsert_note_failed[{idx}]: {exc}")

    if out["note_ids"]:
        out["ok"] = True
    return out


__all__ = ["ingest_external_source"]
