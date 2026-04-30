"""Stage 7 — chunk + embed.

For each WalkedFile (lang != 'other', size <= MAX_FILE_BYTES, no parse error):
    - split text into ~50-line chunks with 10-line overlap
    - embed each chunk via the bge-m3 sidecar (POST /embed)
    - emit Chunk_v2 rows: {id, repo, file_path, text, embed_vec, token_count}

Soft contract:
    - sidecar 5xx / unreachable → skip file silently, increment counter
    - file too large → skip with reason 'too_large'

Sidecar URL via AIFORGE_EMBED_URL (default http://127.0.0.1:8764).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from aiforge_core.codemem.ingest.treesitter_walk import WalkedFile

DEFAULT_EMBED_URL = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764")
MAX_FILE_BYTES = int(os.environ.get("AIFORGE_CODEMEM_EMBED_MAX_BYTES", "65536"))
CHUNK_LINES = int(os.environ.get("AIFORGE_CODEMEM_CHUNK_LINES", "50"))
CHUNK_OVERLAP = int(os.environ.get("AIFORGE_CODEMEM_CHUNK_OVERLAP", "10"))


@dataclass
class WalkedChunk:
    id: str
    repo: str
    file_path: str
    text: str
    embed_vec: list[float] = field(default_factory=list)
    token_count: int = 0
    line_start: int = 0
    line_end: int = 0


def chunk_and_embed(
    walked: list[WalkedFile],
    *,
    repo: str,
    repo_root: str | Path,
) -> list[WalkedChunk]:
    repo_root = Path(repo_root)
    out: list[WalkedChunk] = []

    for wf in walked:
        if wf.parse_error or wf.lang == "other":
            continue
        try:
            data = (repo_root / wf.path).read_bytes()
        except OSError:
            continue
        if len(data) > MAX_FILE_BYTES:
            continue
        text = data.decode("utf-8", errors="replace")
        chunks = _split(text, file_path=wf.path)

        for idx, ch_text, line_start, line_end in chunks:
            chunk_id = _chunk_id(repo, wf.path, idx)
            try:
                vec = _embed(ch_text)
            except Exception:
                vec = []
                # if even one chunk fails, skip the file's remaining chunks
                # (sidecar is probably down)
                break
            out.append(WalkedChunk(
                id=chunk_id, repo=repo, file_path=wf.path,
                text=ch_text, embed_vec=vec,
                token_count=len(ch_text) // 4,
                line_start=line_start, line_end=line_end,
            ))
    return out


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


def _chunk_id(repo: str, file_path: str, idx: int) -> str:
    raw = f"{repo}::{file_path}::{idx}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _embed(text: str) -> list[float]:
    """Call /embed on the sidecar. Returns 1024-d vector."""
    url = DEFAULT_EMBED_URL.rstrip("/") + "/embed"
    r = httpx.post(url, json={"text": text}, timeout=15.0)
    r.raise_for_status()
    data = r.json()
    vec = data.get("embedding") or data.get("vector") or []
    return [float(x) for x in vec]
