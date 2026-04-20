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

import re

# Chunks sized so a hit spans a ~method-level unit of code/markdown.
# 2500 chars ≈ 600-700 tokens, still fits 5 hits under an 8K-token budget.
CHUNK_CHARS = 2500
CHUNK_OVERLAP = 300

# Java method-signature detection: class methods at indent 4 or less.
# Matches signatures like:
#   private Mono<Void> handleSerialDataAdditionIfNeeded(...)
#   public static void foo(...)
#   @Transactional\n    private ...
_JAVA_METHOD_SIG_RE = re.compile(
    r"^(?: {0,8})(?:@\w+(?:\([^)]*\))?\s*\n(?: {0,8})?)*"
    r"(?:public|private|protected|static|final|synchronized|abstract|\s)+"
    r"[\w<>\[\],\s?]+\s+\w+\s*\([^)]*\)\s*(?:throws [\w, ]+)?\s*\{",
    re.MULTILINE,
)


@dataclass
class Chunk:
    source: str   # repo-relative path
    text: str
    chunk_id: str


def _chunk_java_by_method(text: str) -> list[str]:
    """Split .java content along method-signature boundaries.

    Falls back to char chunking if no method boundaries found or file is small.
    Each chunk covers one or more contiguous methods up to CHUNK_CHARS.
    """
    if len(text) <= CHUNK_CHARS:
        return [text]

    # Find method start offsets
    starts = [m.start() for m in _JAVA_METHOD_SIG_RE.finditer(text)]
    if len(starts) < 2:
        return _chunk_generic(text)

    # Prepend 0 if first method isn't at the start (preserves class header as its own chunk)
    if starts[0] > 0:
        starts = [0] + starts
    starts.append(len(text))  # sentinel end

    out: list[str] = []
    buf_start = starts[0]
    for i in range(1, len(starts)):
        if starts[i] - buf_start >= CHUNK_CHARS:
            out.append(text[buf_start : starts[i]])
            # overlap: back up ~CHUNK_OVERLAP chars if possible (align to next boundary)
            buf_start = starts[i]
    if buf_start < len(text):
        out.append(text[buf_start:])
    return [c for c in out if c.strip()]


def _chunk_generic(text: str) -> list[str]:
    """Char-based chunker with overlap. Used for markdown, yaml, non-Java."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    out = []
    i = 0
    while i < len(text):
        out.append(text[i : i + CHUNK_CHARS])
        i += CHUNK_CHARS - CHUNK_OVERLAP
    return out


def _chunk_markdown(text: str) -> list[str]:
    """Back-compat name. Dispatches to generic chunker."""
    return _chunk_generic(text)


def _chunk_for_path(path: str, text: str) -> list[str]:
    """Route to the right chunker based on file extension."""
    if path.endswith(".java"):
        return _chunk_java_by_method(text)
    return _chunk_generic(text)


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

    def reindex(
        self,
        sources: list[str] | None = None,
        external_repos: list[tuple[str, Path, list[str]]] | None = None,
    ) -> dict:
        """Rebuild index from scratch.

        external_repos: list of (label, root_path, globs). Each repo's files
        are indexed with source prefixed by `{label}:` so queries can filter
        by repo. Example:
          external_repos=[
              ("posbackend", Path("~/codeRepo/PosPythonBackend").expanduser(),
               ["app/**/*.py", "tests/**/*.py"]),
          ]
        """
        self._ensure_client()
        assert self._client is not None and self._coll is not None
        self._client.delete_collection(self.collection_name)
        self._coll = self._client.create_collection(self.collection_name)

        docs, ids, metas = [], [], []
        repo_file_counts: dict[str, int] = {}

        def _add_files(label: str, root: Path, files: list[Path]):
            repo_file_counts[label] = len(files)
            for f in files:
                try:
                    rel = str(f.relative_to(root))
                except ValueError:
                    rel = str(f)
                src = f"{label}:{rel}" if label else rel
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for i, chunk in enumerate(_chunk_for_path(rel, text)):
                    cid = hashlib.sha1(f"{src}:{i}:{chunk[:80]}".encode()).hexdigest()
                    docs.append(chunk)
                    ids.append(cid)
                    metas.append({"source": src, "chunk": i, "repo": label or "aiforge"})

        # Local AIForgeCrew sources
        local_files = _gather_files(self.repo_root, sources or DEFAULT_SOURCES)
        _add_files("", self.repo_root, local_files)

        # External repos
        for label, root, globs in (external_repos or []):
            root = root.expanduser().resolve()
            ext_files = _gather_files(root, globs)
            _add_files(label, root, ext_files)

        # Chroma caps batches at ~5000; split defensively
        BATCH = 4000
        for i in range(0, len(docs), BATCH):
            self._coll.add(
                documents=docs[i : i + BATCH],
                ids=ids[i : i + BATCH],
                metadatas=metas[i : i + BATCH],
            )
        return {"files_by_repo": repo_file_counts, "total_files": sum(repo_file_counts.values()), "chunks": len(docs)}

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
