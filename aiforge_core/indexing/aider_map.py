"""Embedded Aider RepoMap wrapper for the Doer hot-path.

Phase 2 of AIForgeCrew v5: surface a tree-sitter + PageRank ranked code map
in the Doer system prompt without forking Aider. We instantiate
``aider.repomap.RepoMap`` in-process, call ``get_repo_map`` with the ticket's
allowed files (``chat_files``) plus the rest of the worktree (``other_files``)
and return the rendered text digest.

Aider is an optional runtime dep — if the import fails (not installed in
this venv, version skew, etc.) ``render_repo_map`` returns an empty string
so callers can fall back gracefully. The Doer prompt is built behind the
``AIFORGE_AIDER_REPOMAP_ENABLED`` env flag, so ``""`` simply omits the
section.

Side-effect hygiene:
- Aider's RepoMap calls ``io.tool_output`` / ``io.tool_error`` for verbose
  / error logging. We pass a quiet shim that swallows everything; nothing
  reaches stdout / stderr.
- Aider's progress bars (``Spinner``, ``tqdm``) only fire on the long
  ``get_ranked_tags`` walk through tags-cache miss territory. We set
  ``verbose=False`` and never construct a Spinner ourselves; ``tqdm`` from
  ``aider.repomap`` only writes when ``sys.stderr.isatty()`` and the
  worktree has > 100 files of cache misses, which is fine for orchestrator
  use because launchd captures stderr to a file.
- The tags cache (`.aider.tags.cache.v4`) is written under ``cache_dir``
  if provided, else under the worktree root. Set
  ``AIFORGE_AIDER_CACHE_DIR=/some/writable/path`` to redirect it.
"""
from __future__ import annotations

import contextlib
import io as _stdio
import os
import threading

# Aider's RepoMap.TAGS_CACHE_DIR is a PROCESS-GLOBAL class attribute. Two
# concurrent renders with a cache_dir override would trample each other's saved
# value (A restores B's dir, etc). Serialize the override span under this lock.
_MAP_LOCK = threading.Lock()
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aiforge_core.observability.logging import emit, get_logger


# ─────────────────────────── Public API ────────────────────────────────


@dataclass
class AiderMapConfig:
    """Inputs for one render pass.

    Attributes:
        root: Worktree root (absolute path).
        chat_files: Files the Doer is currently editing (high PageRank
            weight). Should match the ticket's ## Allowed files entries.
        other_files: Rest of the repo (lower weight). Pass an enumerated
            list of source files; do not pass directories.
        map_tokens: Soft token budget for the digest. Aider may slightly
            overshoot; the wrapper does not post-truncate.
        cache_dir: Override location of ``.aider.tags.cache.v4`` (Aider
            writes a SQLite-ish diskcache here). Defaults to ``root``.
        refresh: Aider cache refresh policy — ``auto`` | ``always`` |
            ``never`` | ``files``.
    """

    root: Path
    chat_files: list[str] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)
    map_tokens: int = 1024
    cache_dir: Optional[Path] = None
    refresh: str = "auto"
    # NEW: user_text feeds aider's mentioned_idents / mentioned_fnames
    # extraction → PageRank personalization. This is the mechanism aider
    # uses to know "the user said 'ProductController' so rank pages
    # around that node high". We were not passing it; PageRank fell back
    # to uniform personalisation and returned generic top-K instead of
    # query-specific.
    user_text: str = ""


def _mentions(user_text: str, rel_files: list[str]) -> tuple[set, set]:
    """``(idents, fnames)`` — aider's get_ident_mentions + get_file_mentions.

    Both are pure functions of the user text; this replicates the same
    word-tokenize + filename match logic as base_coder.py.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return set(), set()
    import re as _re
    idents = {w for w in _re.split(r"\W+", user_text) if w and len(w) >= 3}
    words = {w.rstrip(",.!;:?").strip("\"'`*_") for w in user_text.split()}
    fnames: set[str] = set()
    by_basename: dict[str, list[str]] = {}
    for rel in rel_files:
        if rel in words:
            fnames.add(rel)
        bn = os.path.basename(rel)
        if any(c in bn for c in "/._-"):
            by_basename.setdefault(bn, []).append(rel)
    # A basename only resolves when it is UNAMBIGUOUS across the worktree.
    for bn, rels in by_basename.items():
        if len(rels) == 1 and bn in words:
            fnames.add(rels[0])
    return idents, fnames


def _tags_cache_dir(cfg: "AiderMapConfig"):
    """The tags-cache directory, or None when it cannot be created (read-only
    FS / permissions) — aider then falls back to its default under root."""
    cache_dir = cfg.cache_dir or _resolve_cache_dir(cfg.root)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir
    except Exception:  # noqa: BLE001
        return None


@contextlib.contextmanager
def _tags_cache_redirected(RepoMap, cache_dir):
    """Point aider's hardcoded ``<root>/.aider.tags.cache.v4`` at ``cache_dir``.

    It can't be passed as an argument, so the class attribute is mutated —
    under a lock, and restored, so parallel callers don't trample each other.
    """
    if cache_dir is None:
        yield
        return
    saved = getattr(RepoMap, "TAGS_CACHE_DIR", None)
    _MAP_LOCK.acquire()
    try:
        try:
            RepoMap.TAGS_CACHE_DIR = str(cache_dir / ".aider.tags.cache.v4")
        except Exception:  # noqa: BLE001
            pass
        yield
    finally:
        if saved is not None:
            try:
                RepoMap.TAGS_CACHE_DIR = saved
            except Exception:  # noqa: BLE001
                pass
        _MAP_LOCK.release()


def _run_repomap(RepoMap, cfg, chat_files, other_files, main_model,
                 aider_io) -> str:
    """Build the ranked digest.

    stdout is suppressed: aider internals (and a third-party tree-sitter
    grammar emitting "language not found" on import) write stray lines there.
    stderr goes to the per-role launchd log and is fine.
    """
    rel_files = [os.path.relpath(p, str(cfg.root))
                 for p in (chat_files + other_files)]
    idents, fnames = _mentions(cfg.user_text, rel_files)
    with contextlib.redirect_stdout(_stdio.StringIO()):
        rm = RepoMap(map_tokens=cfg.map_tokens, root=str(cfg.root),
                     main_model=main_model, io=aider_io, verbose=False,
                     refresh=cfg.refresh)
        return rm.get_repo_map(chat_files, other_files,
                               mentioned_fnames=fnames or None,
                               mentioned_idents=idents or None) or ""


def render_repo_map(cfg: AiderMapConfig) -> str:
    """Run Aider's RepoMap and return the ranked text digest.

    Returns an empty string if:
    - Aider is not installed in this Python environment
    - The repo is too small (< 5 files) to bother
    - Aider returns ``None`` (its convention for "nothing useful to map")
    - Any internal exception is raised — we never propagate so the Doer
      prompt assembly cannot fail because of indexing.
    """
    log = get_logger("doer")
    other_files = list(cfg.other_files)
    chat_files = list(cfg.chat_files)
    total = len(chat_files) + len(other_files)
    if total < 5:
        emit(log, "aider_repomap.skipped", reason="repo_too_small",
             file_count=total)
        return ""
    try:
        from aider.repomap import RepoMap  # noqa: WPS433 (deliberately lazy)
    except Exception as exc:  # noqa: BLE001
        emit(log, "aider_repomap.skipped", reason="import_failed",
             error=str(exc)[:200])
        return ""

    main_model = _StubMainModel()
    try:
        with _tags_cache_redirected(RepoMap, _tags_cache_dir(cfg)):
            digest = _run_repomap(RepoMap, cfg, chat_files, other_files,
                                  main_model, _QuietIO())
    except Exception as exc:  # noqa: BLE001 — indexing never breaks the prompt
        emit(log, "aider_repomap.skipped", reason="render_failed",
             error=str(exc)[:200])
        digest = ""

    emit(log, "aider_repomap.rendered",
         token_count=main_model.token_count(digest), file_count=total,
         digest_chars=len(digest))
    return digest


# ─────────────────────────── Mtime cache wrapper ───────────────────────
#
# RepoMap rebuild cost is dominated by tree-sitter parsing. When the
# Doer fires twice on the same worktree without any file changes we
# burn ~300-800ms on identical output. Cache key = (root, git_head,
# frozenset(path:mtime) of chat_files + first 200 other_files). Hit =
# return prior digest verbatim, log cache_hit=1. Toggle via
# AIFORGE_AIDER_MAP_CACHE=0 (default on).

_MAP_CACHE: dict[tuple, str] = {}
_MAP_CACHE_MAX = 16


def render_repo_map_cached(cfg: AiderMapConfig) -> str:
    """Memoised :func:`render_repo_map` keyed on focus-file mtimes.

    Falls back to uncached render on cache lookup error. Bypassed
    entirely when ``AIFORGE_AIDER_MAP_CACHE=0``.
    """
    if os.environ.get("AIFORGE_AIDER_MAP_CACHE", "1") != "1":
        return render_repo_map(cfg)

    key = _cache_key(cfg)
    if key is None:
        return render_repo_map(cfg)

    hit = _MAP_CACHE.get(key)
    log = get_logger("doer")
    if hit is not None:
        emit(log, "aider_repomap.cache_hit", digest_chars=len(hit))
        return hit

    digest = render_repo_map(cfg)
    if digest:
        if len(_MAP_CACHE) >= _MAP_CACHE_MAX:
            _MAP_CACHE.pop(next(iter(_MAP_CACHE)))
        _MAP_CACHE[key] = digest
    return digest


# git HEAD changes rarely; the subprocess (~20-50ms) ran on EVERY
# render_repo_map_cached() call — even cache hits — just to build the key.
# TTL-cache it per root. File mtimes stay in the key, so edits bust the
# map cache regardless of this window.
_HEAD_CACHE: dict[str, tuple[float, str]] = {}
_HEAD_TTL_S = 5.0


def _git_head(root) -> str:
    import time as _time
    k = str(root)
    now = _time.monotonic()
    hit = _HEAD_CACHE.get(k)
    if hit is not None and (now - hit[0]) < _HEAD_TTL_S:
        return hit[1]
    try:
        import subprocess as _sp
        head = _sp.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True,
            timeout=2, check=False,
        ).stdout.strip()
    except Exception:
        head = ""
    _HEAD_CACHE[k] = (now, head)
    return head


def _cache_key(cfg: AiderMapConfig) -> tuple | None:
    """Build hash key from worktree HEAD + focus + focus-file mtimes.

    Returns None on git/stat errors so caller treats it as a miss.
    """
    head = _git_head(cfg.root)
    # Include the focus string: ctx_repomap personalises PageRank on the
    # ticket goal, the Doer on its own work — different focus must NOT share
    # one cached digest (was a silent wrong-context bug).
    focus = getattr(cfg, "user_text", "") or ""

    paths = list(cfg.chat_files) + list(cfg.other_files)[:200]
    fp: list[tuple[str, int]] = []
    for rel in paths:
        try:
            full = cfg.root / rel if not os.path.isabs(rel) else Path(rel)
            mtime_ns = full.stat().st_mtime_ns
            fp.append((rel, mtime_ns))
        except Exception:
            continue
    if not fp:
        return None
    return (str(cfg.root), head, focus, cfg.map_tokens, frozenset(fp))


# ─────────────────────────── Internals ─────────────────────────────────


def _resolve_cache_dir(root: Path) -> Path:
    """Honor AIFORGE_AIDER_CACHE_DIR; default to repo root.

    When AIFORGE_AIDER_CACHE_DIR is set, return a per-repo subdir
    (``<override>/<repo_name>``) so multiple repos don't collide on a
    single Aider tags-cache. AIForge never writes Aider artifacts into
    canonical repo trees by default — cache lives under
    ~/.aiforge/aider-cache/<repo_name>/ on production NUC.
    """
    override = os.environ.get("AIFORGE_AIDER_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser() / Path(root).name
    return Path(root)


class _StubMainModel:
    """Aider's RepoMap calls ``main_model.token_count(text)`` to budget the
    digest. We approximate at ``len(text)//4`` (industry-standard heuristic
    — same one tiktoken uses as a fallback). No real model load.
    """

    # Aider also touches main_model.info / .name occasionally for verbose
    # logging; expose harmless attributes so getattr probes don't blow up.
    name = "aiforge-stub"
    info: dict = {}

    def token_count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)


class _QuietIO:
    """Drop-in shim for ``aider.io.InputOutput``. Swallows any console
    writes the RepoMap emits during walk / cache-rebuild. Returning empty
    strings on prompt methods keeps Aider non-interactive.
    """

    pretty = False
    yes = True

    def tool_output(self, *args, **kwargs) -> None:  # noqa: D401
        return None

    def tool_warning(self, *args, **kwargs) -> None:  # noqa: D401
        return None

    def tool_error(self, *args, **kwargs) -> None:  # noqa: D401
        return None

    def read_text(self, fname: str) -> str:
        try:
            return Path(fname).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def confirm_ask(self, *args, **kwargs) -> bool:
        return False

    def prompt_ask(self, *args, **kwargs) -> str:
        return ""
