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


def _editor_paths(args: dict) -> list[str]:
    if (args.get("command") or "") in {"view"}:
        return []  # read-only, no scope hit
    p = args.get("path") or ""
    return [p] if p else []


def _write_paths(args: dict) -> list[str]:
    p = args.get("path") or args.get("file") or ""
    return [p] if p else []


def _multi_edit_paths(args: dict) -> list[str]:
    # batch edit: {"edits": [{"path", ...}, ...]} — every path counts.
    out: list[str] = []
    for e in (args.get("edits") or []):
        if isinstance(e, dict):
            p = str(e.get("path") or "").strip()
            if p:
                out.append(p)
    return out


def _rename_symbol_paths(args: dict) -> list[str]:
    # Mass word-boundary rewrite across every code file under `path` (default
    # "."). Scope it by its base path so an autonomous run can't rewrite files
    # outside the ticket's allowlist.
    return [str(args.get("path") or ".")]


# tool → paths extractor. Tools absent here (git_commit, reads) hit no scope:
# git_commit touches the whole tree we already approved file by file.
_PATH_EXTRACTORS = {
    "editor": _editor_paths,
    "file_write": _write_paths,
    "file_create": _write_paths,
    "file_patch": _write_paths,
    "multi_edit": _multi_edit_paths,
    "rename_symbol": _rename_symbol_paths,
}


def _path_from_args(tool_name: str, args: dict) -> list[str]:
    """Return every filesystem path the call wants to touch."""
    extractor = _PATH_EXTRACTORS.get(tool_name)
    return extractor(args) if extractor else []


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


# ── chat workspace jail ────────────────────────────────────────────────────
# A chat session's cwd is a DEFAULT, not a sandbox: a mutating file tool given
# an absolute path writes wherever it points. That is how a session opened to
# ask about one thing ended up editing an unrelated repo it had only READ about
# (in recall). ON by default: mutating file tools may only write inside the
# session's own cwd. AIFORGE_CHAT_WORKSPACE_JAIL=0 turns it off for a session
# that legitimately writes outside its cwd (a sibling repo, a path elsewhere on
# the box) — the refusal names the workspace, so a run that hits it says exactly
# what to set.
_JAIL_ENV = "AIFORGE_CHAT_WORKSPACE_JAIL"
_JAIL_OFF = ("0", "false", "no", "off")


def workspace_jail_on() -> bool:
    """True when the chat workspace jail is enabled (the default)."""
    val = (os.environ.get(_JAIL_ENV, "1") or "").strip().lower()
    # Explicit empty ("JAIL=") reads as unset, not as off — an env var cleared
    # by a wrapper script must not silently drop the guard.
    return (val or "1") not in _JAIL_OFF


def outside_workspace(tool_name: str, args: dict, cwd: str | None) -> list[str]:
    """The paths ``tool_name`` wants to WRITE that resolve outside ``cwd``.

    Empty when the jail is off (AIFORGE_CHAT_WORKSPACE_JAIL=0), when there is
    no cwd, or when every target is inside it. A session with no cwd is NOT
    jailed — there is nothing to jail it to; the guard is the cwd, so a caller
    that wants the boundary must give the session one.

    Only the mutating file tools in :data:`_PATH_EXTRACTORS` are
    inspected — reads elsewhere stay allowed (looking at another repo is fine;
    editing it unasked is the bug). Relative paths resolve against ``cwd``, so
    they are inside by construction; an absolute path is what gets caught.
    Symlinks are resolved, so a link inside the workspace pointing out is
    blocked too. Soft-fail: any error → [] (never block on a matcher bug).

    Same KNOWN LIMIT as the glob guard above: shell tools are not parsed.
    """
    if not cwd or not workspace_jail_on():
        return []
    try:
        root = os.path.realpath(str(cwd))
    except Exception as exc:  # noqa: BLE001
        log.debug("workspace jail: bad cwd %r (allow): %s", cwd, exc)
        return []
    out: list[str] = []
    for raw in _path_from_args(tool_name, args or {}):
        try:
            target = os.path.realpath(os.path.join(root, str(raw)))
        except Exception:  # noqa: BLE001
            continue
        if target != root and not target.startswith(root + os.sep):
            out.append(str(raw))
    return out


__all__ = ["make_scope_guard_callback", "outside_workspace", "workspace_jail_on"]
