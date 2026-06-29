"""OpenHands-parity multi-command file editor.

Single entry point :func:`editor` with a ``command`` dispatcher. Supports
``view``, ``create``, ``str_replace``, ``insert``, ``undo_edit``. Soft-
error contract: every failure returns ``{ok: False, error}``; nothing
raises into the model loop.

Snapshots taken BEFORE every mutation land in ``~/.aiforge/editor_undo/``
with a ring depth of 5 per absolute path; :func:`undo_edit` restores the
most recent snapshot and pops it. ``syntax_guard.validate_syntax`` still
gates create/str_replace/insert (respect ``AIFORGE_DOER_SKIP_SYNTAX``).
``sandbox.resolve_inside_root`` blocks path traversal in every command.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

from aiforge_core.runtime.sandbox import resolve_inside_root, root
from aiforge_core.runtime.syntax_guard import validate_syntax

from ._trace import emit

_DIR_LISTING_DEPTH = 2
_UNDO_RING_DEPTH = 5


def _record_touch(path: str) -> None:
    """Record a mutated path in the Doer touched-file tracker so the
    commit/PR step stages only what changed. Lazy import + soft-fail so
    the editor stays usable even if doer_tools can't load."""
    try:
        from aiforge_core.runtime.doer_tools import record_touch
        record_touch(path)
    except Exception:  # noqa: BLE001
        pass


def _undo_dir_for(abs_path: Path) -> Path:
    sha = hashlib.sha1(str(abs_path).encode("utf-8")).hexdigest()
    base = Path.home() / ".aiforge" / "editor_undo" / sha
    base.mkdir(parents=True, exist_ok=True)
    return base


def _push_snapshot(abs_path: Path) -> Path:
    """Capture pre-mutation content (or empty for new files); prune ring."""
    snap_dir = _undo_dir_for(abs_path)
    if abs_path.is_file():
        body = abs_path.read_text(encoding="utf-8", errors="replace")
    else:
        body = ""
    ts = int(time.time() * 1000)
    snap_path = snap_dir / f"{ts}.txt"
    # Avoid collision when two snapshots land in the same millisecond.
    while snap_path.exists():
        ts += 1
        snap_path = snap_dir / f"{ts}.txt"
    snap_path.write_text(body, encoding="utf-8")
    # Mark snapshots taken when the file DIDN'T EXIST, so undo of a `create`
    # deletes the file rather than leaving an empty one behind.
    if not abs_path.is_file():
        (snap_dir / f"{snap_path.stem}.absent").write_text("", encoding="utf-8")
    snaps = sorted(snap_dir.glob("*.txt"))
    while len(snaps) > _UNDO_RING_DEPTH:
        (snap_dir / f"{snaps[0].stem}.absent").unlink(missing_ok=True)
        snaps[0].unlink(missing_ok=True)
        snaps = snaps[1:]
    return snap_path


def _safe_resolve(path: str) -> tuple[Path | None, str | None]:
    try:
        return resolve_inside_root(path or ""), None
    except PermissionError:
        return None, "path_traversal"


def _dir_tree(path: Path, depth: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if depth <= 0:
        return out
    for child in sorted(path.iterdir()):
        kind = "dir" if child.is_dir() else ("file" if child.is_file() else "other")
        try:
            rel = str(child.relative_to(root()))
        except ValueError:
            rel = str(child)
        out.append({"name": child.name, "path": rel, "kind": kind})
    return out


def _view(path: str, view_range: list[int] | None) -> dict[str, Any]:
    p, err = _safe_resolve(path)
    if err:
        return {"ok": False, "error": err, "path": path}
    if not p.exists():
        return {"ok": False, "error": "not_found", "path": path}
    if p.is_dir():
        entries = _dir_tree(p, depth=_DIR_LISTING_DEPTH)
        return {"ok": True, "path": path or ".", "entries": entries}
    if not p.is_file():
        return {"ok": False, "error": "not_a_file_or_dir", "path": path}
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if view_range:
        if len(view_range) != 2:
            return {"ok": False, "error": "invalid_view_range", "path": path}
        start, end = view_range
        if start < 1 or end < start:
            return {"ok": False, "error": "invalid_view_range", "path": path}
        content = "".join(lines[start - 1 : end])
    else:
        content = text
    return {"ok": True, "path": path, "content": content, "total_lines": total}


def _syntax_check(path: str, content: str) -> tuple[bool, str | None]:
    if os.environ.get("AIFORGE_DOER_SKIP_SYNTAX", "0") in ("1", "true"):
        return True, None
    ok, err = validate_syntax(path, content)
    if not ok:
        return False, f"syntax_invalid: {err}"
    return True, None


def _create(path: str, file_text: str | None) -> dict[str, Any]:
    if file_text is None:
        return {"ok": False, "error": "missing_file_text", "path": path}
    p, err = _safe_resolve(path)
    if err:
        return {"ok": False, "error": err, "path": path}
    if p.exists():
        return {"ok": False, "error": "exists", "path": path}
    ok, err_msg = _syntax_check(path, file_text)
    if not ok:
        return {"ok": False, "error": err_msg, "path": path,
                "hint": "fix syntax and call editor create again"}
    _push_snapshot(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(file_text, encoding="utf-8")
    _record_touch(path)
    return {"ok": True, "path": path, "bytes": len(file_text.encode("utf-8"))}


def _str_replace(
    path: str, old_str: str | None, new_str: str | None,
) -> dict[str, Any]:
    if old_str is None or new_str is None:
        return {"ok": False, "error": "missing_old_or_new_str", "path": path}
    p, err = _safe_resolve(path)
    if err:
        return {"ok": False, "error": err, "path": path}
    if not p.is_file():
        return {"ok": False, "error": "not_found", "path": path}
    body = p.read_text(encoding="utf-8")
    count = body.count(old_str)
    if count == 0:
        return {"ok": False, "error": "old_text_not_found", "path": path}
    if count > 1:
        return {"ok": False, "error": "ambiguous_match",
                "path": path, "occurrences": count}
    new_body = body.replace(old_str, new_str, 1)
    ok, err_msg = _syntax_check(path, new_body)
    if not ok:
        return {"ok": False, "error": err_msg, "path": path,
                "hint": "fix syntax and call editor str_replace again"}
    _push_snapshot(p)
    p.write_text(new_body, encoding="utf-8")
    _record_touch(path)
    return {"ok": True, "path": path, "replaced": True}


def _insert(
    path: str, insert_line: int | None, new_str: str | None,
) -> dict[str, Any]:
    if insert_line is None or new_str is None:
        return {"ok": False, "error": "missing_insert_line_or_new_str",
                "path": path}
    p, err = _safe_resolve(path)
    if err:
        return {"ok": False, "error": err, "path": path}
    if not p.is_file():
        return {"ok": False, "error": "not_found", "path": path}
    body = p.read_text(encoding="utf-8")
    lines = body.splitlines(keepends=True)
    if insert_line < 0 or insert_line > len(lines):
        return {"ok": False, "error": "line_out_of_range",
                "path": path, "total_lines": len(lines)}
    new_lines = lines[:insert_line] + [new_str] + lines[insert_line:]
    new_body = "".join(new_lines)
    ok, err_msg = _syntax_check(path, new_body)
    if not ok:
        return {"ok": False, "error": err_msg, "path": path,
                "hint": "fix syntax and call editor insert again"}
    _push_snapshot(p)
    p.write_text(new_body, encoding="utf-8")
    _record_touch(path)
    return {"ok": True, "path": path, "inserted_at": insert_line}


def _undo_edit(path: str) -> dict[str, Any]:
    p, err = _safe_resolve(path)
    if err:
        return {"ok": False, "error": err, "path": path}
    if not p.exists():
        return {"ok": False, "error": "not_found", "path": path}
    snap_dir = _undo_dir_for(p)
    snaps = sorted(snap_dir.glob("*.txt"))
    if not snaps:
        return {"ok": False, "error": "no_history", "path": path}
    most_recent = snaps[-1]
    absent_marker = snap_dir / f"{most_recent.stem}.absent"
    if absent_marker.exists():
        # The file didn't exist before this edit (it was a `create`) — undo by
        # removing it, not by restoring an empty file.
        p.unlink(missing_ok=True)
        most_recent.unlink(missing_ok=True)
        absent_marker.unlink(missing_ok=True)
        emit("EditorUndo", {"path": path, "deleted": True})
        return {"ok": True, "path": path, "deleted": True}
    body = most_recent.read_text(encoding="utf-8")
    p.write_text(body, encoding="utf-8")
    most_recent.unlink(missing_ok=True)
    emit("EditorUndo", {"path": path, "restored_from": most_recent.name})
    return {"ok": True, "path": path, "restored_from": most_recent.name}


def _load_editor_commands_for_role(role: str) -> list[str] | None:
    """Return ``editor_commands`` allowlist for ``role`` from agents.yaml.

    Returns ``None`` for full access (Doer); empty list on parse error
    (fail-closed); list when restricted.
    """
    try:
        from aiforge_core.agents.loader import load_agents
        contracts = load_agents()
        c = contracts.get(role)
        if c is None:
            return None
        return getattr(c, "editor_commands", None)
    except Exception:  # noqa: BLE001 — fail-closed
        return []


def editor(
    command: str,
    path: str = "",
    *,
    file_text: str | None = None,
    old_str: str | None = None,
    new_str: str | None = None,
    insert_line: int | None = None,
    view_range: list[int] | None = None,
    _agent_role: str | None = None,
) -> dict[str, Any]:
    """Dispatcher for the OpenHands-parity file editor.

    Args:
      command: one of ``view``, ``create``, ``str_replace``, ``insert``,
        ``undo_edit``.
      path: repo-relative path.
      file_text: full content for ``create``.
      old_str: search string for ``str_replace``.
      new_str: replacement text for ``str_replace`` / ``insert``.
      insert_line: 0-indexed line for ``insert`` (0 = top of file).
      view_range: ``[start, end]`` 1-indexed inclusive for ``view``.
      _agent_role: agent identity (sub-command allowlist enforcement).
        Passed by ADK ``tool_before_callback``; absent = Doer default.
    """
    if _agent_role:
        allowed = _load_editor_commands_for_role(_agent_role)
        if allowed is not None and command not in allowed:
            return {"ok": False, "error": "editor_command_not_allowed",
                    "command": command, "role": _agent_role}
    if command == "view":
        return _view(path, view_range)
    if command == "create":
        return _create(path, file_text)
    if command == "str_replace":
        return _str_replace(path, old_str, new_str)
    if command == "insert":
        return _insert(path, insert_line, new_str)
    if command == "undo_edit":
        return _undo_edit(path)
    return {"ok": False, "error": "unknown_command", "command": command}
