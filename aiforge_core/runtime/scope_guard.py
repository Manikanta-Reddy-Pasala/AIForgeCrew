"""Diff-scope guard (standards gap C6).

Reject Doer edits that fall outside the ticket's
``scope_allowlist_globs``. The field already lives on
``agents.yaml`` Doer ``contract.inputs`` but nothing enforces it —
this module is the enforcement.

KISS: one function builds an ADK ``before_tool_callback`` for the
Doer LlmAgent. The callback inspects ``editor`` / ``file_write`` /
``file_patch`` calls, extracts the target path(s), checks each
against the glob allowlist, and rejects with a tool-error when any
path falls outside.

KNOWN LIMIT: ``bash`` / ``run_shell`` / ``execute_ipython_cell`` are
NOT inspected — a shell ``sed -i`` can write outside scope. Catching
that requires command parsing; the Validator's scope_ok check is the
backstop for shell-side drift.

Empty allowlist → allow everything (back-compat for tickets that
don't carry the field yet).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger("aiforge.scope_guard")


def _ticket_scope_globs(state: dict) -> list[str]:
    raw = state.get("scope_allowlist_globs") or []
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",") if p.strip()]
    return [g for g in raw if isinstance(g, str) and g]


def _path_from_args(tool_name: str, args: dict) -> list[str]:
    """Return every filesystem path the call wants to touch."""
    if tool_name == "editor":
        if (args.get("command") or "") in {"view"}:
            return []  # read-only, no scope hit
        p = args.get("path") or ""
        return [p] if p else []
    if tool_name in ("file_write", "file_create", "file_patch"):
        p = args.get("path") or args.get("file") or ""
        return [p] if p else []
    if tool_name == "multi_edit":
        # batch edit: {"edits": [{"path", ...}, ...]} — every path counts.
        out: list[str] = []
        for e in (args.get("edits") or []):
            if isinstance(e, dict):
                p = str(e.get("path") or "").strip()
                if p:
                    out.append(p)
        return out
    if tool_name == "git_commit":
        # commit touches the whole tree we already approved file by file
        return []
    return []


def _repo_root_prefixes() -> tuple[str, ...]:
    """Repo-root prefixes to strip when normalizing an absolute path to
    repo-relative — the same env vars the Doer tools resolve their cwd from
    (workspace first, then repo root)."""
    out: list[str] = []
    for var in ("AIFORGE_WORKSPACE_DIR", "AIFORGE_REPO_ROOT"):
        v = os.environ.get(var)
        if v:
            out.append(v.replace("\\", "/").rstrip("/"))
    return tuple(out)


def _norm_path(path: str) -> str:
    """Normalize a target path to repo-relative, forward-slashed form.

    Strips a leading ``AIFORGE_WORKSPACE_DIR`` / ``AIFORGE_REPO_ROOT``
    prefix (so an absolute editor path matches a repo-relative glob), then
    any leading ``./`` and ``/``.
    """
    p = str(path or "").replace("\\", "/").strip()
    for root in _repo_root_prefixes():
        if root and p.startswith(root + "/"):
            p = p[len(root) + 1:]
            break
        if root and p == root:
            p = ""
            break
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _norm_glob(glob: str) -> str:
    g = str(glob or "").replace("\\", "/").strip()
    while g.startswith("./"):
        g = g[2:]
    return g.lstrip("/")


def _glob_to_regex(glob: str) -> str:
    """Translate a glob to an anchored regex with SEGMENT-aware semantics:
    ``**/`` optionally spans nested dirs, ``**`` spans anything, ``*`` /
    ``?`` stay within a single path segment (never cross ``/``)."""
    i, n, out = 0, len(glob), []
    while i < n:
        if glob[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif glob[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "^" + "".join(out) + "$"


def _one_glob_matches(path: str, glob: str) -> bool:
    """True if repo-relative ``path`` matches ``glob``, treating a bare
    directory glob (``src`` / ``src/``) as "everything under it". Mirrors the
    leniency ``parallel_subtasks._in_scope`` had (``g`` + ``g + '/*'``) but
    with proper ``**`` / directory handling so the two matchers agree."""
    g = _norm_glob(glob)
    if not g:
        return False
    stripped = g.rstrip("/")
    # ``g``: exact / file / **-glob match.
    # ``stripped + '/**'``: directory glob → any file under the dir (direct
    # AND nested), since ``**`` → ``.*`` spans ``/``.
    for pat in (g, stripped + "/**"):
        try:
            if re.match(_glob_to_regex(pat), path):
                return True
        except re.error:
            continue
    return False


def _matches_any(path: str, globs) -> bool:
    """True if ``path`` matches ANY allowlist glob. Empty/None → allow all.

    Robust against the shapes the planner commonly emits: directory globs,
    ``**`` globs, and absolute paths under the repo root (normalized to
    repo-relative first). Soft-fail: any internal error → allow (never
    block the Doer on a matcher bug)."""
    try:
        if not globs:
            return True
        norm = _norm_path(path)
        # Match the normalized form and the raw path (back-compat for callers
        # that already pass a repo-relative path).
        candidates = {norm, str(path or "").replace("\\", "/").lstrip("/")}
        for g in globs:
            for c in candidates:
                if _one_glob_matches(c, g):
                    return True
        return False
    except Exception as exc:  # noqa: BLE001 — never block on a matcher error
        log.debug("scope_guard matcher error (allow): %s", exc)
        return True


def make_scope_guard_callback():
    """Return an ADK ``before_tool_callback`` that enforces scope.

    Returning a dict from the callback short-circuits the tool with
    that dict as the response — ADK propagates it as the tool result
    without invoking the real function.
    """
    if os.environ.get("AIFORGE_SCOPE_GUARD", "1") in {"0", "false", ""}:
        return None

    async def _cb(*, tool, args, tool_context, **_kw):
        try:
            state = getattr(tool_context, "state", None) or {}
            globs = _ticket_scope_globs(state)
            if not globs:
                return None  # no allowlist → no enforcement
            tool_name = getattr(tool, "name", "") or ""
            paths = _path_from_args(tool_name, args or {})
            offenders = [p for p in paths if not _matches_any(p, globs)]
            if offenders:
                log.warning(
                    "scope_guard.block tool=%s paths=%s globs=%s",
                    tool_name, offenders, globs,
                )
                return {
                    "ok": False,
                    "error": "scope_violation",
                    "blocked_paths": offenders,
                    "scope_allowlist_globs": globs,
                    "hint": (
                        "Edit refused: path is outside the ticket's "
                        "scope_allowlist_globs. Edit only files inside "
                        "an allowed glob; if the fix genuinely needs a "
                        "wider scope, say so in your verdict rationale "
                        "so the operator can widen the allowlist."
                    ),
                }
        except Exception as exc:  # noqa: BLE001
            log.debug("scope_guard internal error (allow): %s", exc)
        return None

    return _cb


__all__ = ["make_scope_guard_callback"]
