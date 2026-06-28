"""Stage 7 — chunk + embed.

For each WalkedFile (lang != 'other', size <= MAX_FILE_BYTES, no parse error):
    - split text into ~50-line chunks with 10-line overlap
    - embed each chunk via the bge-m3 sidecar (POST /embed)
    - emit Chunk_v2 rows: {id, repo, file_path, text, token_count}

Soft contract:
    - sidecar 5xx / unreachable → skip file silently, increment counter
    - file too large → skip with reason 'too_large'

Embedding model/sidecar are pluggable via env (see ``embed_config``):
    AIFORGE_EMBED_MODEL  (default "bge-m3")
    AIFORGE_EMBED_URL    (default http://127.0.0.1:8764)
    AIFORGE_EMBED_DIM    (default 1024)
Defaults reproduce the historical bge-m3 / 1024-d behaviour exactly.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from aiforge_memory.features.symbol.extract import WalkedFile

# Historical defaults — kept as module constants for backward compat.
DEFAULT_EMBED_MODEL = "bge-m3"
DEFAULT_EMBED_DIM = 1024
DEFAULT_EMBED_URL = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764")
MAX_FILE_BYTES = int(os.environ.get("AIFORGE_CODEMEM_EMBED_MAX_BYTES", "65536"))
# Docs (markdown / rst / adoc / txt) get a bigger budget — CLAUDE.md
# style operations runbooks routinely run 30–50 KB and lose value if
# truncated. CHUNK_LINES is also higher for docs since prose flows.
DOC_MAX_FILE_BYTES = int(os.environ.get("AIFORGE_CODEMEM_DOC_MAX_BYTES", "262144"))
CHUNK_LINES = int(os.environ.get("AIFORGE_CODEMEM_CHUNK_LINES", "50"))
DOC_CHUNK_LINES = int(os.environ.get("AIFORGE_CODEMEM_DOC_CHUNK_LINES", "60"))
CHUNK_OVERLAP = int(os.environ.get("AIFORGE_CODEMEM_CHUNK_OVERLAP", "10"))


def embed_config() -> dict:
    """Resolve the embedding model/sidecar from env (read at call time).

    Returns ``{"model", "url", "dim"}``. Defaults reproduce the historical
    bge-m3 / 1024-d behaviour, so calling with no env set is a no-op change.
    """
    return {
        "model": os.environ.get("AIFORGE_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        "url": os.environ.get("AIFORGE_EMBED_URL", DEFAULT_EMBED_URL),
        "dim": int(os.environ.get("AIFORGE_EMBED_DIM", str(DEFAULT_EMBED_DIM))),
    }


@dataclass
class WalkedChunk:
    id: str
    repo: str
    file_path: str
    text: str
    token_count: int = 0
    line_start: int = 0
    line_end: int = 0


def chunk_and_embed(
    walked: list[WalkedFile],
    *,
    repo: str,
    repo_root: str | Path,
) -> tuple[list[WalkedChunk], list[str]]:
    """Chunk every eligible file. NO embedding.

    Code-chunk vectors were removed (2026-06-11): their only consumer
    was the translator's NL→file anchoring, which a fulltext index over
    chunk text covers at zero embed cost — while embedding was the
    slowest ingest stage, went stale between ingests, and coupled
    ingest to the embed sidecar. Observation_v2 vectors (NL memory)
    are unaffected.

    Returns ``(chunks, failed_paths)`` — the tuple shape is kept for
    caller compatibility; with no embedding step ``failed_paths`` is
    always empty."""
    repo_root = Path(repo_root)
    out: list[WalkedChunk] = []
    failed_paths: list[str] = []

    for wf in walked:
        if wf.parse_error or wf.lang == "other":
            continue
        try:
            data = (repo_root / wf.path).read_bytes()
        except OSError:
            continue
        is_doc = wf.lang.startswith("doc-")
        cap = DOC_MAX_FILE_BYTES if is_doc else MAX_FILE_BYTES
        if len(data) > cap:
            continue
        text = data.decode("utf-8", errors="replace")
        chunks = (
            _split_doc(text, file_path=wf.path)
            if is_doc
            else _split(text, file_path=wf.path)
        )

        for idx, ch_text, line_start, line_end in chunks:
            out.append(WalkedChunk(
                id=_chunk_id(repo, wf.path, idx),
                repo=repo, file_path=wf.path,
                text=ch_text,
                token_count=len(ch_text) // 4,
                line_start=line_start, line_end=line_end,
            ))
    return out, failed_paths


def _split(text: str, *, file_path: str) -> list[tuple[int, str, int, int]]:
    lines = text.splitlines()
    if not lines:
        return []
    out: list[tuple[int, str, int, int]] = []
    step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
    idx = 0
    i = 0
    while i < len(lines):
        chunk_lines = lines[i:i + CHUNK_LINES]
        if not chunk_lines:
            break
        ch_text = "\n".join(chunk_lines)
        # keep small chunks too — rare but valid
        out.append((idx, ch_text, i + 1, i + len(chunk_lines)))
        idx += 1
        if i + CHUNK_LINES >= len(lines):
            break
        i += step
    return out


def _split_doc(text: str, *, file_path: str) -> list[tuple[int, str, int, int]]:
    """Heading-aware doc chunker.

    Markdown/RST/AsciiDoc files are best chunked by heading because
    sections are semantically self-contained. Falls back to line-window
    splitting when there are no headings (long prose). Each chunk gets
    its parent heading prefixed so embedded queries can match on
    "JWT auth" → the section that owns it."""
    lines = text.splitlines()
    if not lines:
        return []
    # Identify heading line indices: # / ## / === / --- / .. ::
    heading_idx: list[int] = []
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith("#") and not s.startswith("#!"):
            heading_idx.append(i)
            continue
        # RST underline: title is preceded by current line, followed
        # by all-=-or---- line. Catch the underline.
        if i > 0 and len(s) >= 3 and set(s) <= set("=-~^*\""):
            heading_idx.append(i - 1)
    heading_idx.append(len(lines))  # sentinel

    out: list[tuple[int, str, int, int]] = []
    if len(heading_idx) <= 1:
        # No headings — fall back to plain windowing.
        step = max(1, DOC_CHUNK_LINES - CHUNK_OVERLAP)
        i = 0
        idx = 0
        while i < len(lines):
            ch = lines[i:i + DOC_CHUNK_LINES]
            if not ch:
                break
            out.append((idx, "\n".join(ch), i + 1, i + len(ch)))
            idx += 1
            if i + DOC_CHUNK_LINES >= len(lines):
                break
            i += step
        return out

    # Section-by-section. Cap each section at ~2 × DOC_CHUNK_LINES;
    # split big sections via window inside.
    idx = 0
    cap = DOC_CHUNK_LINES * 2
    for k in range(len(heading_idx) - 1):
        start = heading_idx[k]
        end = heading_idx[k + 1]
        sect_lines = lines[start:end]
        if not sect_lines:
            continue
        if len(sect_lines) <= cap:
            out.append(
                (idx, "\n".join(sect_lines), start + 1, end),
            )
            idx += 1
            continue
        # Big section — windowed sub-split, each chunk prefixed with
        # the heading so retrieval still associates back.
        head = sect_lines[0]
        i = 0
        step = max(1, DOC_CHUNK_LINES - CHUNK_OVERLAP)
        while i < len(sect_lines):
            sub = sect_lines[i:i + DOC_CHUNK_LINES]
            if not sub:
                break
            text_ch = (head + "\n" + "\n".join(sub)) if i > 0 else "\n".join(sub)
            out.append(
                (idx, text_ch, start + i + 1, start + i + len(sub)),
            )
            idx += 1
            if i + DOC_CHUNK_LINES >= len(sect_lines):
                break
            i += step
    return out


def _chunk_id(repo: str, file_path: str, idx: int) -> str:
    raw = f"{repo}::{file_path}::{idx}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]
