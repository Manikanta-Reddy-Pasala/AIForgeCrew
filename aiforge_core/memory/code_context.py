"""Code-context fetchers for the Doer prompt.

**RepoMap** — local in-process call to ``aiforge_core.indexing.repomap``
(vendored from aider-chat, Apache-2.0; the package itself is not a dep).
Tree-sitter PageRank-ranked tag digest. Hot path. Token-budgeted.

Best-effort. If the lib is unavailable, the function returns "" and the
Doer falls back to ticket body only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from aiforge_core.config.paths import config_dir


# ─────────────── RepoMap (process-local, hot path) ──────────────────────
def _repo_index_dir(root) -> "Path":
    """Central, persistent per-repo index folder — ~/.aiforge/repo-index/<key>.
    Key = the repo's git common dir (shared by ALL worktrees of the repo) so a
    repo is indexed ONCE and reused across sessions/worktrees; falls back to the
    real path when not a git repo. AIFORGE_REPO_INDEX_DIR overrides the base."""
    import hashlib
    import subprocess
    from pathlib import Path as _P
    r = _P(root)
    ident = str(r.resolve())
    try:
        cg = subprocess.run(["git", "-C", str(r), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
        if cg:
            p = _P(cg)
            if not p.is_absolute():
                p = (r / cg)
            ident = str(p.resolve().parent)      # the repo's real root
    except Exception:  # noqa: BLE001
        pass
    key = hashlib.sha1(ident.encode()).hexdigest()[:16]
    base = os.environ.get("AIFORGE_REPO_INDEX_DIR") or os.path.join(
        str(config_dir()),
        "repo-index")
    d = _P(base) / key
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


def aider_digest(worktree: str, chat_files: list[str],
                 token_budget: int = 1024,
                 user_text: str = "") -> str:
    """Run the ranked RepoMap on the worktree and return its digest.

    ``user_text`` is the raw natural-language request — RepoMap extracts
    `mentioned_idents` (every word) and `mentioned_fnames` (basename
    matches against the repo) from it and uses them as PageRank
    personalisation. Without it the digest is generic top-K; with it
    the digest centres on what the user actually asked about.

    Returns "" on any error (grammars unavailable, repo too small, etc).
    Caller injects the result verbatim into the Doer system prompt.
    """
    if os.environ.get("AIFORGE_AIDER_REPOMAP_ENABLED", "1") != "1":
        return ""
    try:
        from aiforge_core.indexing.aider_map import (
            AiderMapConfig, render_repo_map_cached as render_repo_map,
        )
    except Exception:
        return ""
    root = Path(worktree)
    other = _enumerate_repo_files(root, exclude=set(chat_files))
    cfg = AiderMapConfig(
        root=root,
        chat_files=chat_files,
        other_files=other,
        map_tokens=token_budget,
        user_text=user_text,
        # PERSISTENT central tags cache keyed by the REAL repo (git-common-dir),
        # so worktrees of the same repo SHARE it and a repo is scanned ONCE — not
        # re-scanned on every fresh worktree/session. ~/.aiforge/repo-index/<key>.
        cache_dir=_repo_index_dir(root),
    )
    try:
        digest = render_repo_map(cfg) or ""
    except Exception:
        return ""
    return digest


_REPO_MAP_EXTS = (".java", ".py", ".ts", ".tsx", ".js", ".kt", ".go")


def _wanted_repo_file(dirpath: str, fname: str, root: Path, exclude: set[str],
                      is_noise_path) -> "str | None":
    """The absolute path of ``fname`` if it is an in-scope code file, else None
    (wrong extension, noise path, or explicitly excluded)."""
    if not fname.endswith(_REPO_MAP_EXTS):
        return None
    full = os.path.join(dirpath, fname)
    if is_noise_path(full):
        return None
    if os.path.relpath(full, root) in exclude:
        return None
    return full


def _enumerate_repo_files(root: Path, exclude: set[str],
                          cap: int = 4000) -> list[str]:
    """Walk worktree, return code files. Noise dirs/extensions filtered
    via the shared ``aiforge_core.indexing.noise`` module — single source
    of truth across all indexers + retrievers."""
    from aiforge_core.indexing.noise import prune_dirnames, is_noise_path
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        prune_dirnames(dirnames)   # in-place prune for noise dirs
        for f in filenames:
            full = _wanted_repo_file(dirpath, f, root, exclude, is_noise_path)
            if full is None:
                continue
            out.append(full)
            if len(out) >= cap:
                return out
    return out


# ─────────────────── Compat shim (understander / legacy callers) ────────────
def query(text: str, *, repo: str = "", token_budget: int = 4000) -> str:
    """Thin compat wrapper for callers that use the old 1-arg API.

    Tries the AiForgeMemory API first (if installed), otherwise falls back
    to an empty string so the Understander degrades gracefully.
    """
    # Module renamed api/read.py → api/http.py in AiForgeMemory commit
    # 32d86ad. Try the new name first, fall back to the old.
    try:
        try:
            from aiforge_memory.api.http import context_bundle_for  # type: ignore
        except ImportError:
            from aiforge_memory.api.read import context_bundle_for  # type: ignore
        return context_bundle_for(text, repo=repo, role="any",
                                  token_budget=token_budget)
    except Exception:
        return ""
