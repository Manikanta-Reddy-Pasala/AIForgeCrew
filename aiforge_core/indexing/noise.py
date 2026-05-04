"""Single source of truth for indexing noise.

Every indexer + retrieval path imports from here so a path that is
noise to RepoMap is also noise to graphify ingest, treesitter ingest,
merkle hashing, symbol embedding, AND the UnifiedContext output
filter. KISS: one constants module, two helper functions.

Also exposes a Cypher fragment for one-shot purge of pre-existing
:Symbol / :Chunk / :File nodes whose ``file_path`` matches noise so
old crud can be cleaned out without re-indexing.
"""
from __future__ import annotations

import os


# ─────────── Directory names (basename match in os.walk) ───────────
EXCLUDE_DIRS: frozenset[str] = frozenset({
    # VCS / IDE
    ".git", ".svn", ".hg", ".idea", ".vscode",
    # Build outputs
    "target", "build", "out", "dist", "generated", "generated-sources",
    "bin", "obj", "_build",
    # Package managers / vendor
    "node_modules", "vendor", "bower_components", ".pnpm-store",
    # Python envs / caches
    ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    # Java / Gradle / Maven caches
    ".gradle", ".mvn", ".m2",
    # Aider / AIForge runtime caches
    ".aider.tags.cache.v4", ".aider.cache.json",
    ".aiforge", ".aiforge-worktrees",
})


# ─────────── Path substring patterns (full-path containment) ───────
# Used when callers only have a full path string (no walk context).
EXCLUDE_DIR_TOKENS: tuple[str, ...] = tuple(
    f"/{d}/" for d in EXCLUDE_DIRS
) + (
    # Generated POM files (Maven flatten plugin) — common false-positive
    "/.flattened-pom.xml",
    "/flattened-pom.xml",
)


# ─────────── File extensions to drop ───────────
EXCLUDE_EXTENSIONS: frozenset[str] = frozenset({
    # Compiled / binary
    ".pyc", ".pyo", ".pyd", ".class", ".jar", ".war", ".ear",
    ".so", ".dll", ".dylib", ".o", ".obj", ".a",
    # Bundled / minified
    ".min.js", ".min.css", ".map",
    # Lock files (huge churn, never useful for code reasoning)
    ".lock",
    # Image / media (we never need to index)
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".mp4", ".mov", ".webm", ".pdf", ".zip", ".tar", ".gz",
})


# ─────────── Helpers ───────────


def is_noise_dir(name: str) -> bool:
    """True for directory basenames the indexers must NOT descend into."""
    return name in EXCLUDE_DIRS


def is_noise_path(path: str) -> bool:
    """True when the full path lives inside a noise dir OR ends in a
    noise extension. Path separators normalised to forward slash so
    Windows paths work too (rare but cheap)."""
    if not path:
        return True
    p = path.replace("\\", "/").lower()
    if any(t in p for t in EXCLUDE_DIR_TOKENS):
        return True
    if any(p.endswith(ext) for ext in EXCLUDE_EXTENSIONS):
        return True
    return False


def filter_paths(paths) -> list:
    """Return only paths that pass is_noise_path. Order preserved."""
    return [p for p in paths if not is_noise_path(p)]


def prune_dirnames(dirnames: list[str]) -> None:
    """In-place mutation for ``os.walk`` callers — the canonical idiom
    is ``dirnames[:] = ...`` so the walker skips pruned subtrees."""
    dirnames[:] = [d for d in dirnames if not is_noise_dir(d)]


# ─────────── Neo4j purge fragment ───────────
# Cypher template that nukes :Symbol / :Chunk / :File whose
# ``file_path`` lives in a noise dir. Run once after deploying the
# shared filter to drop pre-existing crud. Not auto-run — operator
# triggers via ``aiforge index purge-noise`` (added separately).
PURGE_CYPHER = """
UNWIND $tokens AS tok
MATCH (n)
WHERE (n:Symbol OR n:Chunk OR n:File OR n:Memory)
  AND n.file_path CONTAINS tok
DETACH DELETE n
RETURN tok, count(n) AS purged
"""


__all__ = [
    "EXCLUDE_DIRS", "EXCLUDE_DIR_TOKENS", "EXCLUDE_EXTENSIONS",
    "is_noise_dir", "is_noise_path", "filter_paths",
    "prune_dirnames", "PURGE_CYPHER",
]
