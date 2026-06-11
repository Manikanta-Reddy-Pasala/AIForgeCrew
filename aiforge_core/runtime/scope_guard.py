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

import fnmatch
import logging
import os
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
    if tool_name == "file_write" or tool_name == "file_patch":
        p = args.get("path") or args.get("file") or ""
        return [p] if p else []
    if tool_name == "git_commit":
        # commit touches the whole tree we already approved file by file
        return []
    return []


def _matches_any(path: str, globs: list[str]) -> bool:
    # Match both with / without leading ``./`` so callers can be lazy.
    candidates = (path, path.lstrip("./"))
    for g in globs:
        for c in candidates:
            if fnmatch.fnmatch(c, g):
                return True
    return False


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
