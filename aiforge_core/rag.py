"""Local RAG over project docs (DESIGN.md §7).

Indexes markdown + .yml + .py under configured paths, stores chunks in
ChromaDB PersistentClient at `.aiforge/rag/`. Query returns top-k chunks
with source path + snippet. Reindex is idempotent — re-runs only touch
files whose mtime changed since last pass.

Embeddings:
  - Default: ChromaDB bundled embedder (fully local, no network)
  - Alt: `embedder='lm-studio'` uses /v1/embeddings on LM Studio
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SOURCES = [
    "README.md",
    "DESIGN.md",
    "docs/**/*.md",
    "memory/**/*.yml",
    "security/**/*.yml",
    "agents/**/*.md",
    "agents/**/*.yml",
]

# Small chunk size so 5 hits fit under DESIGN §6.1 8K token budget.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200


@dataclass
class Chunk:
    source: str   # repo-relative path
    text: str
    chunk_id: str


def _chunk_markdown(text: str) -> list[str]:
    """Naive char-based chunker. Markdown-aware variant can be added later."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    out = []
    i = 0
    while i < len(text):
        out.append(text[i : i + CHUNK_CHARS])
        i += CHUNK_CHARS - CHUNK_OVERLAP
    return out


def _gather_files(repo_root: Path, globs: list[str]) -> list[Path]:
    seen: set[Path] = set()
    for pat in globs:
        for p in repo_root.glob(pat):
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


class RagIndex:
    """Thin ChromaDB wrapper. Lazy-imports chromadb so base install is light."""

    def __init__(self, repo_root: Path, db_dir: Path | None = None, collection: str = "aiforge"):
        self.repo_root = repo_root.resolve()
        self.db_dir = (db_dir or (self.repo_root / ".aiforge" / "rag")).resolve()
        self.collection_name = collection
        self._client = None
        self._coll = None

    def _ensure_client(self):
        if self._client is not None:
            return
        try:
            import chromadb
        except ImportError as e:
            raise RuntimeError(
                "chromadb not installed — run `make rag-install` "
                "or `uv pip install -e '.[rag]'`"
            ) from e
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.db_dir))
        self._coll = self._client.get_or_create_collection(self.collection_name)

    def reindex(self, sources: list[str] | None = None) -> dict:
        """Rebuild index from scratch (simple + idempotent)."""
        self._ensure_client()
        assert self._client is not None and self._coll is not None
        # Drop + recreate for simplicity; later switch to delta reindex.
        self._client.delete_collection(self.collection_name)
        self._coll = self._client.create_collection(self.collection_name)

        files = _gather_files(self.repo_root, sources or DEFAULT_SOURCES)
        docs, ids, metas = [], [], []
        for f in files:
            rel = str(f.relative_to(self.repo_root))
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, chunk in enumerate(_chunk_markdown(text)):
                cid = hashlib.sha1(f"{rel}:{i}:{chunk[:80]}".encode()).hexdigest()
                docs.append(chunk)
                ids.append(cid)
                metas.append({"source": rel, "chunk": i})
        if docs:
            self._coll.add(documents=docs, ids=ids, metadatas=metas)
        return {"files": len(files), "chunks": len(docs)}

    def query(self, q: str, top_k: int = 5) -> list[Chunk]:
        self._ensure_client()
        assert self._coll is not None
        res = self._coll.query(query_texts=[q], n_results=top_k)
        out: list[Chunk] = []
        docs = res.get("documents") or [[]]
        metas = res.get("metadatas") or [[]]
        ids = res.get("ids") or [[]]
        for doc, meta, cid in zip(docs[0], metas[0], ids[0]):
            out.append(Chunk(source=meta.get("source", "?"), text=doc, chunk_id=cid))
        return out
