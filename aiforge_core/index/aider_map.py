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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aiforge_core.runtime.logging_setup import emit, get_logger


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
        emit(
            log,
            "aider_repomap.skipped",
            reason="repo_too_small",
            file_count=total,
        )
        return ""

    try:
        from aider.repomap import RepoMap  # noqa: WPS433 (deliberately lazy)
    except Exception as exc:
        emit(
            log,
            "aider_repomap.skipped",
            reason="import_failed",
            error=str(exc)[:200],
        )
        return ""

    cache_dir = cfg.cache_dir or _resolve_cache_dir(cfg.root)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Read-only FS / permission issue — let Aider fall back to its
        # default location relative to root, or warn-and-skip.
        cache_dir = None  # type: ignore[assignment]

    main_model = _StubMainModel()
    aider_io = _QuietIO()

    # Aider's RepoMap reads/writes its tags cache from
    # ``<root>/.aider.tags.cache.v4``. We can't pass cache_dir directly —
    # it's hardcoded — so we redirect it via TAGS_CACHE_DIR mutation if
    # the override is set. Keep the mutation scoped via try/finally so
    # parallel callers don't trample.
    saved_tags_cache_dir = getattr(RepoMap, "TAGS_CACHE_DIR", None)
    if cache_dir is not None:
        try:
            RepoMap.TAGS_CACHE_DIR = str(cache_dir / ".aider.tags.cache.v4")
        except Exception:
            pass

    digest = ""
    try:
        # Suppress any stray stdout writes from Aider internals (e.g. a
        # third-party tree-sitter grammar emitting "language not found"
        # warnings on import). stderr goes to launchd log per role and
        # is fine.
        with contextlib.redirect_stdout(_stdio.StringIO()):
            rm = RepoMap(
                map_tokens=cfg.map_tokens,
                root=str(cfg.root),
                main_model=main_model,
                io=aider_io,
                verbose=False,
                refresh=cfg.refresh,
            )
            result = rm.get_repo_map(chat_files, other_files)
        digest = result or ""
    except Exception as exc:
        emit(
            log,
            "aider_repomap.skipped",
            reason="render_failed",
            error=str(exc)[:200],
        )
        digest = ""
    finally:
        if cache_dir is not None and saved_tags_cache_dir is not None:
            try:
                RepoMap.TAGS_CACHE_DIR = saved_tags_cache_dir
            except Exception:
                pass

    emit(
        log,
        "aider_repomap.rendered",
        token_count=main_model.token_count(digest),
        file_count=total,
        digest_chars=len(digest),
    )
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


def _cache_key(cfg: AiderMapConfig) -> tuple | None:
    """Build hash key from worktree HEAD + focus-file mtimes.

    Returns None on git/stat errors so caller treats it as a miss.
    """
    try:
        import subprocess as _sp
        head = _sp.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cfg.root), capture_output=True, text=True,
            timeout=2, check=False,
        ).stdout.strip()
    except Exception:
        head = ""

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
    return (str(cfg.root), head, cfg.map_tokens, frozenset(fp))


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
