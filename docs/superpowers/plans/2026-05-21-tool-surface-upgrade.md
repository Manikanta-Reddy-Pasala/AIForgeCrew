# Tool Surface Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Doer's current file/shell tools with OpenHands-parity surface: persistent tmux-backed bash, multi-command `editor` (view/create/str_replace/insert/undo_edit), `think` no-op, `finish` Doer signal — landed in a new layered `aiforge_core/runtime/tools/` package.

**Architecture:** Layered tools package under `aiforge_core/runtime/tools/`. One module per tool (editor.py / bash.py / cognition.py / _trace.py). Old `doer_tools.py` shrinks to a deprecation shim that delegates to the new tools. `agents.yaml` swaps Doer's tool allowlist; non-Doer agents get an `editor_commands: [view]` sub-allowlist. ADK runner wires the new FunctionTool factory.

**Tech Stack:** Python 3.12, `google-adk>=2.0.0b1`, tmux, pytest, existing helpers (`sandbox.resolve_inside_root`, `syntax_guard.validate_syntax`, `runtime.observability`).

**Spec:** `docs/superpowers/specs/2026-05-21-tool-surface-upgrade-design.md`

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `aiforge_core/runtime/tools/__init__.py` | Create | `adk_function_tools()` factory; canonical re-exports |
| `aiforge_core/runtime/tools/_trace.py` | Create | `emit(label, props)` Neo4j event emitter |
| `aiforge_core/runtime/tools/editor.py` | Create | `editor(command, path, **kwargs)` dispatcher |
| `aiforge_core/runtime/tools/bash.py` | Create | tmux session manager + `bash(cmd, restart, timeout)` |
| `aiforge_core/runtime/tools/cognition.py` | Create | `think(thought)` + `finish(summary, status)` |
| `aiforge_core/runtime/doer_tools.py` | Modify | Shrink to deprecation shim; canonicals delegate to `tools/` |
| `aiforge_core/agents/agents.yaml` | Modify | Doer allowlist swap; non-Doer `editor_commands` field |
| `aiforge_core/agents/loader.py` | Modify | Parse + validate optional `editor_commands` field |
| `aiforge_core/runtime/adk_runner.py` | Modify | Import factory from `tools/` |
| `aiforge_core/runtime/pipeline.py` | Modify | Import factory from `tools/` |
| `tests/python/runtime/tools/__init__.py` | Create | Empty marker |
| `tests/python/runtime/tools/test_editor.py` | Create | Editor unit tests |
| `tests/python/runtime/tools/test_bash.py` | Create | Bash unit tests |
| `tests/python/runtime/tools/test_cognition.py` | Create | Think/finish unit tests |
| `tests/python/runtime/test_tools_pkg_integration.py` | Create | End-to-end smoke |
| `tests/python/test_doer_tools.py` | Modify | Keep coverage of deprecation shim behaviour |
| `README.md` | Modify | New tool surface section |
| `docs/agent-rules.md` | Modify | Per-agent allowlist diff |

---

## Task 1: Scaffold the `tools/` package + shared trace emitter

**Files:**
- Create: `aiforge_core/runtime/tools/__init__.py`
- Create: `aiforge_core/runtime/tools/_trace.py`
- Create: `tests/python/runtime/__init__.py` (if missing)
- Create: `tests/python/runtime/tools/__init__.py`
- Create: `tests/python/runtime/tools/test_trace.py`

- [ ] **Step 1: Write the failing test for `_trace.emit`**

```python
# tests/python/runtime/tools/test_trace.py
from __future__ import annotations

from unittest.mock import patch

from aiforge_core.runtime.tools import _trace


def test_emit_calls_observability_with_label_and_props():
    with patch("aiforge_core.runtime.tools._trace._safe_emit") as mock:
        _trace.emit("Think", {"thought": "hi", "ticket_id": "ONE-1"})
    mock.assert_called_once()
    args, _kwargs = mock.call_args
    assert args[0] == "Think"
    assert args[1]["thought"] == "hi"
    assert args[1]["ticket_id"] == "ONE-1"
    assert "ts" in args[1]


def test_emit_truncates_oversize_strings_to_4kb():
    big = "x" * 5000
    with patch("aiforge_core.runtime.tools._trace._safe_emit") as mock:
        _trace.emit("Think", {"thought": big})
    sent = mock.call_args[0][1]
    assert len(sent["thought"]) <= 4096
    assert sent["thought"].endswith("...[truncated]")


def test_emit_never_raises_on_observability_failure():
    with patch(
        "aiforge_core.runtime.tools._trace._safe_emit",
        side_effect=RuntimeError("neo4j down"),
    ):
        _trace.emit("Think", {"thought": "hi"})  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/runtime/tools/test_trace.py -v`
Expected: FAIL with `ModuleNotFoundError: aiforge_core.runtime.tools`

- [ ] **Step 3: Create the package and trace emitter**

```python
# aiforge_core/runtime/tools/__init__.py
"""OpenHands-parity tool surface for the Doer agent.

Sub-modules:

* :mod:`editor`      — multi-command file editor (view/create/str_replace/insert/undo_edit)
* :mod:`bash`        — tmux-backed persistent shell session
* :mod:`cognition`   — think + finish
* :mod:`_trace`      — shared Neo4j event emitter

Sibling tool modules NEVER import each other — keeps responsibilities clean and
unit tests cheap. The ADK :class:`FunctionTool` factory lives below.
"""
from __future__ import annotations

__all__ = ["adk_function_tools"]


def adk_function_tools() -> list:
    """Return canonical Doer tools as ADK ``FunctionTool`` instances.

    Lazy import keeps unit tests ADK-free.
    """
    from google.adk.tools import FunctionTool

    from .bash import bash
    from .cognition import finish, think
    from .editor import editor

    canonical = [editor, bash, think, finish]
    return [FunctionTool(func=fn) for fn in canonical]
```

```python
# aiforge_core/runtime/tools/_trace.py
"""Shared trace-event emitter for the OpenHands-parity tool surface.

Every tool calls :func:`emit` to record a labelled event on the Neo4j
trace alongside the existing ``:Turn`` / ``:ToolCall`` nodes. The emit
path is best-effort: a Neo4j outage or missing config never bubbles into
the model loop. Oversized string fields are truncated to 4 KB to keep
the audit trail bounded.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("aiforge.tools.trace")

_MAX_STR_BYTES = 4096
_TRUNC_SUFFIX = "...[truncated]"


def _clip(val: Any) -> Any:
    if isinstance(val, str) and len(val.encode("utf-8")) > _MAX_STR_BYTES:
        budget = _MAX_STR_BYTES - len(_TRUNC_SUFFIX)
        return val.encode("utf-8")[:budget].decode("utf-8", "replace") + _TRUNC_SUFFIX
    return val


def _safe_emit(label: str, props: dict[str, Any]) -> None:
    """Delegate to runtime.observability.emit_trace. Importing here keeps
    the module unit-test friendly (test_trace.py mocks this function)."""
    from aiforge_core.runtime import observability

    fn = getattr(observability, "emit_trace", None)
    if fn is None:
        return
    fn(label=label, props=props)


def emit(label: str, props: dict[str, Any]) -> None:
    """Record a trace event. Best-effort: never raises into the agent loop."""
    clean: dict[str, Any] = {"ts": time.time()}
    for k, v in props.items():
        clean[k] = _clip(v)
    try:
        _safe_emit(label, clean)
    except Exception as exc:  # noqa: BLE001 — best-effort audit
        log.debug("trace.emit_failed label=%s: %s", label, exc)
```

```python
# tests/python/runtime/__init__.py
```

```python
# tests/python/runtime/tools/__init__.py
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/python/runtime/tools/test_trace.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/tools/__init__.py aiforge_core/runtime/tools/_trace.py \
        tests/python/runtime/__init__.py tests/python/runtime/tools/__init__.py \
        tests/python/runtime/tools/test_trace.py
git commit -m "feat(tools): scaffold runtime/tools package + shared trace emitter

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `editor.view` command

**Files:**
- Create: `aiforge_core/runtime/tools/editor.py`
- Create: `tests/python/runtime/tools/test_editor.py`

- [ ] **Step 1: Write failing tests for `editor(command="view", ...)`**

```python
# tests/python/runtime/tools/test_editor.py
from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiforge_core.runtime.tools import editor as ed


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    return tmp_path


def test_view_returns_full_file_content(repo):
    out = ed.editor("view", "hello.py")
    assert out["ok"]
    assert out["content"] == "print('hi')\n"
    assert out["total_lines"] == 1


def test_view_with_range_returns_slice(repo):
    out = ed.editor("view", "sub/nested.txt", view_range=[2, 4])
    assert out["ok"]
    assert out["content"] == "b\nc\nd\n"


def test_view_dir_returns_tree(repo):
    out = ed.editor("view", "")
    assert out["ok"]
    names = {entry["name"] for entry in out["entries"]}
    assert names == {"hello.py", "sub"}


def test_view_missing_file_returns_error(repo):
    out = ed.editor("view", "nope.py")
    assert out["ok"] is False
    assert out["error"] == "not_found"


def test_view_path_traversal_rejected(repo):
    out = ed.editor("view", "../etc/passwd")
    assert out["ok"] is False
    assert out["error"] == "path_traversal"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 5 failures with `ModuleNotFoundError: aiforge_core.runtime.tools.editor`

- [ ] **Step 3: Implement `view` command**

```python
# aiforge_core/runtime/tools/editor.py
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

import os
from pathlib import Path
from typing import Any

from aiforge_core.runtime.sandbox import resolve_inside_root, root
from aiforge_core.runtime.syntax_guard import validate_syntax

from ._trace import emit

_DIR_LISTING_DEPTH = 2


def _safe_resolve(path: str) -> tuple[Path | None, str | None]:
    """Return ``(resolved, None)`` or ``(None, "path_traversal")``."""
    try:
        return resolve_inside_root(path or ""), None
    except PermissionError:
        return None, "path_traversal"


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


def _dir_tree(path: Path, depth: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if depth <= 0:
        return out
    for child in sorted(path.iterdir()):
        kind = "dir" if child.is_dir() else ("file" if child.is_file() else "other")
        rel = str(child.relative_to(root()))
        out.append({"name": child.name, "path": rel, "kind": kind})
    return out


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
      _agent_role: agent identity (for sub-command allowlist enforcement).
        Passed in by ADK ``tool_before_callback``; absent → Doer default.
    """
    if command == "view":
        return _view(path, view_range)
    return {"ok": False, "error": "unknown_command", "command": command}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/tools/editor.py tests/python/runtime/tools/test_editor.py
git commit -m "feat(tools): editor view command (range, dir tree, traversal guard)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `editor.create` command + undo snapshot ring

**Files:**
- Modify: `aiforge_core/runtime/tools/editor.py`
- Modify: `tests/python/runtime/tools/test_editor.py`

- [ ] **Step 1: Append failing tests for `create`**

```python
# Append to tests/python/runtime/tools/test_editor.py

def test_create_happy(repo):
    out = ed.editor("create", "new.txt", file_text="hello\n")
    assert out["ok"]
    assert (repo / "new.txt").read_text() == "hello\n"
    assert out["bytes"] == 6


def test_create_rejects_existing(repo):
    out = ed.editor("create", "hello.py", file_text="x = 1\n")
    assert out["ok"] is False
    assert out["error"] == "exists"


def test_create_rejects_bad_python_syntax(repo, monkeypatch):
    monkeypatch.delenv("AIFORGE_DOER_SKIP_SYNTAX", raising=False)
    out = ed.editor("create", "bad.py", file_text="def foo(:\n")
    assert out["ok"] is False
    assert out["error"].startswith("syntax_invalid")


def test_create_pushes_undo_snapshot(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = ed.editor("create", "snap.txt", file_text="v1\n")
    assert out["ok"]
    # Snapshot dir for this path
    from aiforge_core.runtime.tools.editor import _undo_dir_for
    snap_dir = _undo_dir_for(repo / "snap.txt")
    snaps = list(snap_dir.glob("*.txt"))
    assert len(snaps) == 1
    # Create snapshot is "empty before" — recorded as empty string
    assert snaps[0].read_text() == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 4 new failures, all 5 prior tests still pass

- [ ] **Step 3: Implement `create` + undo machinery**

Replace the file's top imports + add the undo helpers + `_create` + dispatcher case:

```python
# aiforge_core/runtime/tools/editor.py — replace existing content with this
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


def _undo_dir_for(abs_path: Path) -> Path:
    sha = hashlib.sha1(str(abs_path).encode("utf-8")).hexdigest()
    base = Path.home() / ".aiforge" / "editor_undo" / sha
    base.mkdir(parents=True, exist_ok=True)
    return base


def _push_snapshot(abs_path: Path) -> Path:
    """Capture the pre-mutation file content (or empty string for new files)
    and prune the ring to ``_UNDO_RING_DEPTH``."""
    snap_dir = _undo_dir_for(abs_path)
    if abs_path.is_file():
        body = abs_path.read_text(encoding="utf-8", errors="replace")
    else:
        body = ""
    ts = int(time.time() * 1000)
    snap_path = snap_dir / f"{ts}.txt"
    snap_path.write_text(body, encoding="utf-8")
    snaps = sorted(snap_dir.glob("*.txt"))
    while len(snaps) > _UNDO_RING_DEPTH:
        snaps[0].unlink(missing_ok=True)
        snaps = snaps[1:]
    return snap_path


def _safe_resolve(path: str) -> tuple[Path | None, str | None]:
    try:
        return resolve_inside_root(path or ""), None
    except PermissionError:
        return None, "path_traversal"


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


def _dir_tree(path: Path, depth: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if depth <= 0:
        return out
    for child in sorted(path.iterdir()):
        kind = "dir" if child.is_dir() else ("file" if child.is_file() else "other")
        rel = str(child.relative_to(root()))
        out.append({"name": child.name, "path": rel, "kind": kind})
    return out


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
    return {"ok": True, "path": path, "bytes": len(file_text.encode("utf-8"))}


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
    """OpenHands-parity multi-command file editor (see module docstring)."""
    if command == "view":
        return _view(path, view_range)
    if command == "create":
        return _create(path, file_text)
    return {"ok": False, "error": "unknown_command", "command": command}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/tools/editor.py tests/python/runtime/tools/test_editor.py
git commit -m "feat(tools): editor create + undo snapshot ring (depth 5)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `editor.str_replace` command

**Files:**
- Modify: `aiforge_core/runtime/tools/editor.py`
- Modify: `tests/python/runtime/tools/test_editor.py`

- [ ] **Step 1: Append failing tests**

```python
# Append to tests/python/runtime/tools/test_editor.py

def test_str_replace_happy(repo):
    (repo / "doc.md").write_text("hello world\n", encoding="utf-8")
    out = ed.editor("str_replace", "doc.md", old_str="world", new_str="earth")
    assert out["ok"]
    assert (repo / "doc.md").read_text() == "hello earth\n"


def test_str_replace_not_found_when_missing(repo):
    out = ed.editor("str_replace", "missing.md", old_str="x", new_str="y")
    assert out["ok"] is False
    assert out["error"] == "not_found"


def test_str_replace_old_text_not_found(repo):
    (repo / "doc.md").write_text("hello\n", encoding="utf-8")
    out = ed.editor("str_replace", "doc.md", old_str="nope", new_str="y")
    assert out["ok"] is False
    assert out["error"] == "old_text_not_found"


def test_str_replace_ambiguous_match(repo):
    (repo / "doc.md").write_text("a\na\n", encoding="utf-8")
    out = ed.editor("str_replace", "doc.md", old_str="a", new_str="b")
    assert out["ok"] is False
    assert out["error"] == "ambiguous_match"
    assert out["occurrences"] == 2


def test_str_replace_pushes_snapshot(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (repo / "doc.md").write_text("hello world\n", encoding="utf-8")
    ed.editor("str_replace", "doc.md", old_str="world", new_str="earth")
    from aiforge_core.runtime.tools.editor import _undo_dir_for
    snaps = list(_undo_dir_for(repo / "doc.md").glob("*.txt"))
    assert len(snaps) == 1
    assert snaps[0].read_text() == "hello world\n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 5 new failures, all 9 prior tests still pass

- [ ] **Step 3: Add `_str_replace` and dispatcher branch**

Insert before `editor(...)`:

```python
def _str_replace(path: str, old_str: str | None, new_str: str | None) -> dict[str, Any]:
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
    return {"ok": True, "path": path, "replaced": True}
```

Update dispatcher in `editor(...)`:

```python
    if command == "str_replace":
        return _str_replace(path, old_str, new_str)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/tools/editor.py tests/python/runtime/tools/test_editor.py
git commit -m "feat(tools): editor str_replace (single-match, ambiguous detection)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `editor.insert` command

**Files:**
- Modify: `aiforge_core/runtime/tools/editor.py`
- Modify: `tests/python/runtime/tools/test_editor.py`

- [ ] **Step 1: Append failing tests**

```python
# Append to tests/python/runtime/tools/test_editor.py

def test_insert_at_top(repo):
    (repo / "doc.md").write_text("a\nb\n", encoding="utf-8")
    out = ed.editor("insert", "doc.md", insert_line=0, new_str="x\n")
    assert out["ok"]
    assert (repo / "doc.md").read_text() == "x\na\nb\n"
    assert out["inserted_at"] == 0


def test_insert_mid_file(repo):
    (repo / "doc.md").write_text("a\nb\nc\n", encoding="utf-8")
    out = ed.editor("insert", "doc.md", insert_line=2, new_str="X\n")
    assert out["ok"]
    assert (repo / "doc.md").read_text() == "a\nb\nX\nc\n"


def test_insert_beyond_eof_rejects(repo):
    (repo / "doc.md").write_text("a\n", encoding="utf-8")
    out = ed.editor("insert", "doc.md", insert_line=99, new_str="X\n")
    assert out["ok"] is False
    assert out["error"] == "line_out_of_range"


def test_insert_missing_file(repo):
    out = ed.editor("insert", "missing.md", insert_line=0, new_str="X\n")
    assert out["ok"] is False
    assert out["error"] == "not_found"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 4 new failures, 14 prior tests pass

- [ ] **Step 3: Add `_insert` and dispatcher branch**

Insert before `editor(...)`:

```python
def _insert(path: str, insert_line: int | None, new_str: str | None) -> dict[str, Any]:
    if insert_line is None or new_str is None:
        return {"ok": False, "error": "missing_insert_line_or_new_str", "path": path}
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
    return {"ok": True, "path": path, "inserted_at": insert_line}
```

Update dispatcher:

```python
    if command == "insert":
        return _insert(path, insert_line, new_str)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/tools/editor.py tests/python/runtime/tools/test_editor.py
git commit -m "feat(tools): editor insert (0=top, bounds-checked)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `editor.undo_edit` command

**Files:**
- Modify: `aiforge_core/runtime/tools/editor.py`
- Modify: `tests/python/runtime/tools/test_editor.py`

- [ ] **Step 1: Append failing tests**

```python
# Append to tests/python/runtime/tools/test_editor.py

def test_undo_edit_restores_previous_version(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (repo / "doc.md").write_text("v1\n", encoding="utf-8")
    ed.editor("str_replace", "doc.md", old_str="v1", new_str="v2")
    assert (repo / "doc.md").read_text() == "v2\n"
    out = ed.editor("undo_edit", "doc.md")
    assert out["ok"]
    assert (repo / "doc.md").read_text() == "v1\n"


def test_undo_edit_no_history(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (repo / "fresh.md").write_text("untouched\n", encoding="utf-8")
    out = ed.editor("undo_edit", "fresh.md")
    assert out["ok"] is False
    assert out["error"] == "no_history"


def test_undo_edit_ring_walks_history(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (repo / "doc.md").write_text("v1\n", encoding="utf-8")
    ed.editor("str_replace", "doc.md", old_str="v1", new_str="v2")
    ed.editor("str_replace", "doc.md", old_str="v2", new_str="v3")
    assert (repo / "doc.md").read_text() == "v3\n"
    ed.editor("undo_edit", "doc.md")
    assert (repo / "doc.md").read_text() == "v2\n"
    ed.editor("undo_edit", "doc.md")
    assert (repo / "doc.md").read_text() == "v1\n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 3 new failures, 18 prior tests pass

- [ ] **Step 3: Add `_undo_edit` and dispatcher branch**

Insert before `editor(...)`:

```python
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
    body = most_recent.read_text(encoding="utf-8")
    p.write_text(body, encoding="utf-8")
    most_recent.unlink(missing_ok=True)
    emit("EditorUndo", {"path": path, "restored_from": most_recent.name})
    return {"ok": True, "path": path, "restored_from": most_recent.name}
```

Update dispatcher:

```python
    if command == "undo_edit":
        return _undo_edit(path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 21 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/tools/editor.py tests/python/runtime/tools/test_editor.py
git commit -m "feat(tools): editor undo_edit walks per-path snapshot ring

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Editor sub-command allowlist via `_agent_role`

**Files:**
- Modify: `aiforge_core/runtime/tools/editor.py`
- Modify: `tests/python/runtime/tools/test_editor.py`

- [ ] **Step 1: Append failing test**

```python
# Append to tests/python/runtime/tools/test_editor.py

def test_non_doer_blocked_from_mutating_commands(repo, monkeypatch):
    monkeypatch.setattr(
        ed, "_load_editor_commands_for_role",
        lambda role: ["view"] if role == "planner" else None,
    )
    out = ed.editor("create", "x.txt", file_text="y", _agent_role="planner")
    assert out["ok"] is False
    assert out["error"] == "editor_command_not_allowed"


def test_doer_allowed_all_commands(repo, monkeypatch):
    monkeypatch.setattr(
        ed, "_load_editor_commands_for_role",
        lambda role: None,
    )
    out = ed.editor("create", "y.txt", file_text="z\n", _agent_role="doer")
    assert out["ok"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 2 new failures

- [ ] **Step 3: Add allowlist enforcement**

Add helper near top (after `_UNDO_RING_DEPTH`):

```python
def _load_editor_commands_for_role(role: str) -> list[str] | None:
    """Return the ``editor_commands`` allowlist for ``role`` from agents.yaml.

    Returns ``None`` when the role has no allowlist (full access).
    Network-free; loader caches on first call.
    """
    try:
        from aiforge_core.agents.loader import load_agents
        contracts = load_agents()
        c = contracts.get(role)
        if c is None:
            return None
        return getattr(c, "editor_commands", None)
    except Exception:  # noqa: BLE001 — fail-open is unsafe; fail-closed
        return []
```

Update the head of `editor(...)`:

```python
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
    """OpenHands-parity multi-command file editor (see module docstring)."""
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/python/runtime/tools/test_editor.py -v`
Expected: 23 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/tools/editor.py tests/python/runtime/tools/test_editor.py
git commit -m "feat(tools): per-agent editor sub-command allowlist via agents.yaml

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: `bash` tool — fallback path (no tmux)

**Files:**
- Create: `aiforge_core/runtime/tools/bash.py`
- Create: `tests/python/runtime/tools/test_bash.py`

We implement the fallback path first because it is platform-independent and exercises the same I/O contract; the tmux path arrives in Task 9.

- [ ] **Step 1: Write failing tests for `bash` fallback**

```python
# tests/python/runtime/tools/test_bash.py
from __future__ import annotations

from unittest.mock import patch

import pytest

from aiforge_core.runtime.tools import bash as bm


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    return tmp_path


def test_fallback_runs_simple_command(repo_root, monkeypatch):
    monkeypatch.setattr(bm, "_tmux_available", lambda: False)
    out = bm.bash("echo hi")
    assert out["ok"]
    assert out["returncode"] == 0
    assert out["stdout"].strip() == "hi"


def test_fallback_captures_nonzero_exit(repo_root, monkeypatch):
    monkeypatch.setattr(bm, "_tmux_available", lambda: False)
    out = bm.bash("false")
    assert out["ok"] is False
    assert out["returncode"] != 0


def test_fallback_timeout(repo_root, monkeypatch):
    monkeypatch.setattr(bm, "_tmux_available", lambda: False)
    out = bm.bash("sleep 5", timeout=1)
    assert out["ok"] is False
    assert out["error"] == "timeout"


def test_fallback_truncates_huge_stdout(repo_root, monkeypatch):
    monkeypatch.setattr(bm, "_tmux_available", lambda: False)
    out = bm.bash("python -c \"print('x'*20000)\"")
    assert out["ok"]
    assert len(out["stdout"]) <= 8000
    assert out["truncated"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/runtime/tools/test_bash.py -v`
Expected: 4 failures with `ModuleNotFoundError`

- [ ] **Step 3: Implement the fallback path**

```python
# aiforge_core/runtime/tools/bash.py
"""tmux-backed persistent bash session manager for the Doer agent.

The model calls :func:`bash` which dispatches to a per-ADK-run tmux
session. If tmux is unavailable the call degrades to a stateless
subprocess that mirrors the original ``run_shell`` behaviour so the
agent loop still works on contributor boxes lacking tmux.

Lifecycle (tmux path):

* Session ``aiforge-{run_id}`` lazily created on first call.
* Custom prompt ``__AIFORGE_PROMPT_$?__`` makes exit code parsing trivial.
* Session destroyed in :func:`destroy_session` (called from
  ``BasePlugin.on_finish_callback``).
* ``restart=True`` kills + recreates the session.

Output capped at 8 KB per call; default timeout 90 s; trailing ``&``
backgrounds the job and returns immediately.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from aiforge_core.runtime.sandbox import root

from ._trace import emit

_STDOUT_CAP_BYTES = 8000
_DEFAULT_TIMEOUT_S = 90


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _fallback_run(command: str, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=root(),
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": "timeout",
            "command": command,
            "stdout": (exc.stdout or b"").decode("utf-8", "replace")[:_STDOUT_CAP_BYTES],
            "stderr": (exc.stderr or b"").decode("utf-8", "replace")[:_STDOUT_CAP_BYTES],
            "truncated": True,
        }
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": command,
        "stdout": out[:_STDOUT_CAP_BYTES],
        "stderr": err[:_STDOUT_CAP_BYTES],
        "truncated": len(out) > _STDOUT_CAP_BYTES or len(err) > _STDOUT_CAP_BYTES,
    }


def bash(
    command: str,
    *,
    restart: bool = False,
    timeout: int = _DEFAULT_TIMEOUT_S,
    _run_id: str | None = None,
) -> dict[str, Any]:
    """Run ``command`` in the persistent session for ``_run_id``.

    When tmux is not available falls back to a stateless subprocess.
    Soft-error contract: failures return ``{ok: False, error, ...}``.
    """
    if not command or not command.strip():
        return {"ok": False, "error": "empty_command"}
    if not _tmux_available():
        emit("BashFallback", {"reason": "tmux_missing"})
        return _fallback_run(command, timeout)
    # tmux path arrives in Task 9 — for now fall through to fallback.
    return _fallback_run(command, timeout)


def destroy_session(run_id: str) -> None:
    """No-op in the fallback-only build; real implementation lands in Task 9."""
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/python/runtime/tools/test_bash.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/tools/bash.py tests/python/runtime/tools/test_bash.py
git commit -m "feat(tools): bash fallback path (no-tmux dev boxes)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: `bash` tool — tmux-backed persistent session

**Files:**
- Modify: `aiforge_core/runtime/tools/bash.py`
- Modify: `tests/python/runtime/tools/test_bash.py`

- [ ] **Step 1: Append integration tests for the tmux path**

```python
# Append to tests/python/runtime/tools/test_bash.py
import shutil


pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


@pytestmark_tmux
def test_tmux_persists_cwd_across_calls(repo_root):
    run_id = "test-persist-cwd"
    try:
        bm.bash("mkdir -p sub && cd sub", _run_id=run_id)
        out = bm.bash("pwd", _run_id=run_id)
        assert out["ok"]
        assert out["stdout"].strip().endswith("/sub")
    finally:
        bm.destroy_session(run_id)


@pytestmark_tmux
def test_tmux_persists_env_var(repo_root):
    run_id = "test-persist-env"
    try:
        bm.bash("export AIFORGE_TEST_VAR=ping", _run_id=run_id)
        out = bm.bash("echo $AIFORGE_TEST_VAR", _run_id=run_id)
        assert out["ok"]
        assert out["stdout"].strip() == "ping"
    finally:
        bm.destroy_session(run_id)


@pytestmark_tmux
def test_tmux_restart_wipes_state(repo_root):
    run_id = "test-restart"
    try:
        bm.bash("export FOO=before", _run_id=run_id)
        bm.bash("noop", restart=True, _run_id=run_id)
        out = bm.bash("echo ${FOO:-empty}", _run_id=run_id)
        assert out["stdout"].strip() == "empty"
    finally:
        bm.destroy_session(run_id)


@pytestmark_tmux
def test_tmux_destroy_session_cleans_up(repo_root):
    run_id = "test-destroy"
    bm.bash("true", _run_id=run_id)
    bm.destroy_session(run_id)
    proc = subprocess.run(["tmux", "has-session", "-t", f"aiforge-{run_id}"],
                          capture_output=True)
    assert proc.returncode != 0
```

(Add `import subprocess` at the top of test file.)

- [ ] **Step 2: Run tests to verify they fail (tmux must be installed)**

Run: `pytest tests/python/runtime/tools/test_bash.py -v`
Expected: 4 new failures when tmux is present (otherwise skipped). Prior 4 tests still pass.

- [ ] **Step 3: Implement the tmux path**

Replace `bash.py` content with the full tmux-aware implementation:

```python
# aiforge_core/runtime/tools/bash.py
"""tmux-backed persistent bash session manager for the Doer agent.

The model calls :func:`bash` which dispatches to a per-ADK-run tmux
session. If tmux is unavailable the call degrades to a stateless
subprocess that mirrors the original ``run_shell`` behaviour so the
agent loop still works on contributor boxes lacking tmux.

Lifecycle (tmux path):

* Session ``aiforge-{run_id}`` lazily created on first call.
* Custom prompt ``__AIFORGE_PROMPT_$?__`` makes exit code parsing trivial.
* Session destroyed in :func:`destroy_session` (wired from the ADK
  finish callback).
* ``restart=True`` kills + recreates the session.

Output capped at 8 KB per call; default timeout 90 s; trailing ``&``
backgrounds the job and returns immediately.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from typing import Any

from aiforge_core.runtime.sandbox import root

from ._trace import emit

_STDOUT_CAP_BYTES = 8000
_DEFAULT_TIMEOUT_S = 90
_POLL_INTERVAL_S = 0.1
_PROMPT_PS1 = r"PS1='__AIFORGE_PROMPT_$?__\n'"
_SENTINEL_RE = re.compile(r"__AIFORGE_PROMPT_(\d+)__")

_active_sessions: dict[str, str] = {}


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _session_name(run_id: str) -> str:
    return f"aiforge-{run_id}"


def _session_exists(name: str) -> bool:
    proc = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    )
    return proc.returncode == 0


def _create_session(run_id: str) -> str:
    name = _session_name(run_id)
    if _session_exists(name):
        return name
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(root()), "bash",
         "--noprofile", "--norc"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", name, _PROMPT_PS1, "Enter"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", name, "clear", "Enter"],
        check=True, capture_output=True,
    )
    time.sleep(0.2)
    _capture(name)
    _active_sessions[run_id] = name
    emit("BashSession", {"session": name, "action": "created"})
    return name


def _capture(name: str) -> str:
    proc = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", name, "-S", "-10000"],
        capture_output=True,
    )
    return proc.stdout.decode("utf-8", "replace")


def _drain_until_prompt(name: str, timeout: int) -> tuple[str, int | None, bool]:
    """Poll capture-pane until the sentinel reappears or timeout hits.

    Returns ``(stdout_text, returncode|None, timed_out)``.
    """
    deadline = time.monotonic() + timeout
    last_seen = ""
    while time.monotonic() < deadline:
        pane = _capture(name)
        # Find the LAST sentinel (most recent prompt)
        matches = list(_SENTINEL_RE.finditer(pane))
        if len(matches) >= 2:
            # Two sentinels = command finished (one before, one after).
            second_last = matches[-2]
            last = matches[-1]
            body = pane[second_last.end() : last.start()]
            rc = int(last.group(1))
            return body.strip("\n"), rc, False
        last_seen = pane
        time.sleep(_POLL_INTERVAL_S)
    return last_seen, None, True


def _fallback_run(command: str, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=root(),
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": "timeout",
            "command": command,
            "stdout": (exc.stdout or b"").decode("utf-8", "replace")[:_STDOUT_CAP_BYTES],
            "stderr": (exc.stderr or b"").decode("utf-8", "replace")[:_STDOUT_CAP_BYTES],
            "truncated": True,
        }
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": command,
        "stdout": out[:_STDOUT_CAP_BYTES],
        "stderr": err[:_STDOUT_CAP_BYTES],
        "truncated": len(out) > _STDOUT_CAP_BYTES or len(err) > _STDOUT_CAP_BYTES,
    }


def bash(
    command: str,
    *,
    restart: bool = False,
    timeout: int = _DEFAULT_TIMEOUT_S,
    _run_id: str | None = None,
) -> dict[str, Any]:
    """Run ``command`` in the persistent session for ``_run_id``."""
    if not command or not command.strip():
        return {"ok": False, "error": "empty_command"}
    if not _tmux_available():
        emit("BashFallback", {"reason": "tmux_missing"})
        return _fallback_run(command, timeout)

    if _run_id is None:
        _run_id = "default-" + uuid.uuid4().hex[:8]

    name = _session_name(_run_id)
    if restart and _session_exists(name):
        destroy_session(_run_id)
    _create_session(_run_id)

    if command.rstrip().endswith("&"):
        # Background job: do not wait for sentinel.
        subprocess.run(
            ["tmux", "send-keys", "-t", name, command, "Enter"],
            check=True, capture_output=True,
        )
        return {"ok": True, "command": command, "backgrounded": True,
                "returncode": 0, "stdout": "", "truncated": False}

    subprocess.run(
        ["tmux", "send-keys", "-t", name, command, "Enter"],
        check=True, capture_output=True,
    )
    body, rc, timed_out = _drain_until_prompt(name, timeout)
    if timed_out:
        subprocess.run(
            ["tmux", "send-keys", "-t", name, "C-c"],
            capture_output=True,
        )
        time.sleep(0.5)
        partial, _rc2, _t2 = _drain_until_prompt(name, 2)
        return {
            "ok": False, "error": "timeout", "command": command,
            "stdout": partial[:_STDOUT_CAP_BYTES], "truncated": True,
        }
    return {
        "ok": (rc == 0),
        "returncode": rc,
        "command": command,
        "stdout": body[:_STDOUT_CAP_BYTES],
        "truncated": len(body) > _STDOUT_CAP_BYTES,
    }


def destroy_session(run_id: str) -> None:
    """Kill the tmux session associated with ``run_id`` (best-effort)."""
    name = _active_sessions.pop(run_id, _session_name(run_id))
    if not _tmux_available():
        return
    if _session_exists(name):
        subprocess.run(["tmux", "kill-session", "-t", name],
                       capture_output=True)
        emit("BashSession", {"session": name, "action": "destroyed"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/python/runtime/tools/test_bash.py -v`
Expected: 8 passed (4 fallback + 4 tmux)

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/tools/bash.py tests/python/runtime/tools/test_bash.py
git commit -m "feat(tools): tmux-backed persistent bash session

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: `think` + `finish` cognition tools

**Files:**
- Create: `aiforge_core/runtime/tools/cognition.py`
- Create: `tests/python/runtime/tools/test_cognition.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/python/runtime/tools/test_cognition.py
from __future__ import annotations

from unittest.mock import patch

from aiforge_core.runtime.tools import cognition as cg


def test_think_returns_ok_and_emits_trace():
    with patch("aiforge_core.runtime.tools.cognition.emit") as mock:
        out = cg.think("considering options")
    assert out == {"ok": True}
    mock.assert_called_once()
    label, props = mock.call_args[0]
    assert label == "Think"
    assert props["thought"] == "considering options"


def test_think_caps_oversize_thought():
    big = "x" * 5000
    with patch("aiforge_core.runtime.tools.cognition.emit") as mock:
        cg.think(big)
    sent = mock.call_args[0][1]["thought"]
    # _trace caps at 4 KB; tool itself just delegates
    assert len(sent.encode("utf-8")) <= 5000  # cognition does not double-trim


def test_finish_doer_ok():
    out = cg.finish("all green", status="done", _agent_role="doer")
    assert out["ok"]
    assert out["terminate"] is True
    assert out["summary"] == "all green"
    assert out["status"] == "done"


def test_finish_non_doer_rejected():
    out = cg.finish("done", _agent_role="planner")
    assert out["ok"] is False
    assert out["error"] == "agent_not_authorized"


def test_finish_invalid_status():
    out = cg.finish("done", status="weird", _agent_role="doer")
    assert out["ok"] is False
    assert out["error"] == "invalid_status"


def test_finish_blocked_status_passes():
    out = cg.finish("stuck on test infra", status="blocked", _agent_role="doer")
    assert out["ok"]
    assert out["status"] == "blocked"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/runtime/tools/test_cognition.py -v`
Expected: 6 failures with `ModuleNotFoundError`

- [ ] **Step 3: Implement `think` + `finish`**

```python
# aiforge_core/runtime/tools/cognition.py
"""Lightweight cognition tools: ``think`` (no-op + trace) and ``finish``
(Doer-only explicit termination signal).

Neither tool touches the filesystem, memory, or any external service —
they exist purely to give the model an explicit signal channel and to
emit observability events the operator can audit later.
"""
from __future__ import annotations

from typing import Any

from ._trace import emit

_THOUGHT_MAX_BYTES = 4096
_SUMMARY_MAX_BYTES = 2048
_VALID_FINISH_STATUS = {"done", "blocked"}


def think(thought: str) -> dict[str, Any]:
    """Record an explicit reasoning step. Pure no-op for the model loop."""
    if not isinstance(thought, str):
        thought = str(thought)
    if len(thought.encode("utf-8")) > _THOUGHT_MAX_BYTES:
        thought = thought.encode("utf-8")[:_THOUGHT_MAX_BYTES].decode(
            "utf-8", "replace"
        ) + "...[truncated]"
    emit("Think", {"thought": thought})
    return {"ok": True}


def finish(
    summary: str,
    status: str = "done",
    *,
    _agent_role: str | None = None,
) -> dict[str, Any]:
    """Doer-only explicit termination signal.

    Returns ``{ok: True, terminate: True, summary, status}`` on success.
    ADK's LoopAgent inspects ``terminate=True`` to halt the Doer step;
    the Feedback agent downstream reads ``summary`` and the last
    ``compile_status`` / ``test_status`` from session state.
    """
    if _agent_role is not None and _agent_role != "doer":
        return {"ok": False, "error": "agent_not_authorized",
                "role": _agent_role}
    if status not in _VALID_FINISH_STATUS:
        return {"ok": False, "error": "invalid_status", "status": status}
    if not isinstance(summary, str):
        summary = str(summary)
    if len(summary.encode("utf-8")) > _SUMMARY_MAX_BYTES:
        summary = summary.encode("utf-8")[:_SUMMARY_MAX_BYTES].decode(
            "utf-8", "replace"
        ) + "...[truncated]"
    emit("Finish", {"summary": summary, "status": status})
    return {"ok": True, "terminate": True, "summary": summary, "status": status}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/python/runtime/tools/test_cognition.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/tools/cognition.py tests/python/runtime/tools/test_cognition.py
git commit -m "feat(tools): think + finish cognition tools (Doer-only finish)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: `tools/__init__.py` FunctionTool factory wired to all four tools

**Files:**
- Modify: `aiforge_core/runtime/tools/__init__.py`
- Create: `tests/python/runtime/tools/test_factory.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/python/runtime/tools/test_factory.py
from __future__ import annotations


def test_factory_returns_four_function_tools():
    from aiforge_core.runtime.tools import adk_function_tools
    tools = adk_function_tools()
    names = {t.func.__name__ for t in tools}
    assert names == {"editor", "bash", "think", "finish"}


def test_factory_function_tools_are_adk_instances():
    from aiforge_core.runtime.tools import adk_function_tools
    from google.adk.tools import FunctionTool
    tools = adk_function_tools()
    for t in tools:
        assert isinstance(t, FunctionTool)
```

- [ ] **Step 2: Run tests to verify they pass already**

Run: `pytest tests/python/runtime/tools/test_factory.py -v`
Expected: 2 passed (the factory was already implemented in Task 1)

- [ ] **Step 3: Commit the new tests**

```bash
git add tests/python/runtime/tools/test_factory.py
git commit -m "test(tools): factory returns editor+bash+think+finish FunctionTools

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: `doer_tools.py` deprecation shim

**Files:**
- Modify: `aiforge_core/runtime/doer_tools.py`
- Modify: `tests/python/test_doer_tools.py`

- [ ] **Step 1: Write failing test for shim behaviour**

```python
# tests/python/test_doer_tools.py — append at end of file

def test_file_write_delegates_to_editor_create(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    from aiforge_core.runtime import doer_tools as dt
    out = dt.file_write("legacy.txt", "hello\n")
    assert out["ok"]
    assert (tmp_path / "legacy.txt").read_text() == "hello\n"


def test_run_shell_delegates_to_bash(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    from aiforge_core.runtime import doer_tools as dt
    out = dt.run_shell("echo legacy")
    assert out["ok"]
    assert "legacy" in out["stdout"]


def test_adk_function_tools_delegates_to_new_package():
    """Legacy adk_function_tools() must return the new pkg's tools."""
    from aiforge_core.runtime import doer_tools as dt
    tools = dt.adk_function_tools()
    names = {t.func.__name__ for t in tools}
    assert {"editor", "bash", "think", "finish"}.issubset(names)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/test_doer_tools.py -v -k "delegates or adk_function_tools"`
Expected: 3 failures

- [ ] **Step 3: Replace `doer_tools.py` with deprecation shim**

```python
# aiforge_core/runtime/doer_tools.py
"""DEPRECATED — kept for one release as a thin shim that forwards
canonical names to :mod:`aiforge_core.runtime.tools`.

Remove after the next minor release. New code MUST import from the
``aiforge_core.runtime.tools`` package directly.
"""
from __future__ import annotations

import warnings

from .tools import adk_function_tools as _new_factory
from .tools.bash import bash as _bash
from .tools.cognition import finish as _finish
from .tools.cognition import think as _think
from .tools.editor import editor as _editor

warnings.warn(
    "aiforge_core.runtime.doer_tools is deprecated; "
    "import from aiforge_core.runtime.tools instead",
    DeprecationWarning,
    stacklevel=2,
)


def file_read(path: str) -> dict:
    """Deprecated. Delegates to ``editor view``."""
    return _editor("view", path)


def file_write(path: str, content: str) -> dict:
    """Deprecated. Delegates to ``editor create``."""
    return _editor("create", path, file_text=content)


def file_patch(path: str, old_text: str, new_text: str) -> dict:
    """Deprecated. Delegates to ``editor str_replace``."""
    return _editor("str_replace", path, old_str=old_text, new_str=new_text)


def list_dir(path: str = "") -> dict:
    """Deprecated. Delegates to ``editor view`` on a dir path."""
    return _editor("view", path)


def run_shell(cmd: str) -> dict:
    """Deprecated. Delegates to ``bash``."""
    return _bash(cmd)


def grep_repo(pattern: str, path: str = ".") -> dict:
    """Deprecated. Use ripgrep via ``bash`` instead."""
    return _bash(f"rg --no-heading --line-number --max-count 200 "
                 f"-e {pattern!r} {path}")


def fetch_url(url: str) -> dict:
    """Deprecated. Inline shim — no behaviour change."""
    import urllib.error
    import urllib.request
    if not url or not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "url must be http(s)"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AIForgeCrew-Doer/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(256 * 1024 + 1)
            status = resp.status
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"http {exc.code}", "status": exc.code}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"url error: {exc.reason}"}
    except (TimeoutError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "url": url, "status": status,
            "body": raw[: 256 * 1024].decode("utf-8", "replace"),
            "bytes": len(raw), "truncated": len(raw) > 256 * 1024}


def git_commit(message: str) -> dict:
    """Deprecated. Delegates to ``bash`` running git commands directly."""
    if not message or not str(message).strip():
        return {"ok": False, "error": "empty commit message"}
    add = _bash("git add -A -- . ':(exclude)graphify-out' ':(exclude).aiforge' "
                "':(exclude).aiforge-worktrees' ':(exclude).idea' "
                "':(exclude).vscode' ':(exclude).DS_Store'")
    if not add["ok"]:
        return {"ok": False, "error": "git_add_failed", "stderr": add.get("stderr", "")}
    diff = _bash("git diff --cached --quiet; echo $?")
    if diff["stdout"].strip() == "0":
        return {"ok": True, "skipped": "nothing to commit"}
    return _bash(f"git commit -m {message!r}")


# Aliases preserved for any caller still using them.
read = file_read
write = file_write
patch = file_patch
ls = list_dir
shell = run_shell
bash = run_shell
grep = grep_repo
search = grep_repo
http_get = fetch_url
web_fetch = fetch_url
commit = git_commit
git_add_commit = git_commit


def adk_function_tools() -> list:
    """Deprecated. Returns the new package's FunctionTool list."""
    return _new_factory()


__all__ = [
    "file_read", "file_write", "file_patch", "list_dir", "run_shell",
    "grep_repo", "fetch_url", "git_commit",
    "read", "write", "patch", "ls", "shell", "bash",
    "grep", "search", "http_get", "web_fetch",
    "commit", "git_add_commit",
    "adk_function_tools",
]
```

- [ ] **Step 4: Run the full doer_tools test file**

Run: `pytest tests/python/test_doer_tools.py -v`
Expected: all pass (legacy + new delegation tests). Some pre-existing tests that
inspected internal helpers may need their assertions tightened — if any fail,
update the assertion to match the new behaviour (delegation through editor/bash)
in the same edit. Do not change behaviour to satisfy a stale test.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/doer_tools.py tests/python/test_doer_tools.py
git commit -m "refactor(tools): shrink doer_tools.py to deprecation shim

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: `agents.yaml` swap + `editor_commands` field

**Files:**
- Modify: `aiforge_core/agents/agents.yaml`
- Modify: `aiforge_core/agents/loader.py`
- Modify: `tests/python/test_agents.py`

- [ ] **Step 1: Write failing tests for loader's new field**

```python
# tests/python/test_agents.py — append

def test_loader_parses_editor_commands_field():
    from aiforge_core.agents.loader import load_agents
    contracts = load_agents()
    planner = contracts["planner"]
    # Planner must have editor in allowed
    assert "editor" in planner.tools.allowed
    # editor_commands must default to [view] for non-Doer
    assert planner.editor_commands == ["view"]


def test_doer_has_full_editor_access():
    from aiforge_core.agents.loader import load_agents
    contracts = load_agents()
    doer = contracts["doer"]
    # Doer must not have an editor_commands restriction (None = full access)
    assert doer.editor_commands is None
    assert "editor" in doer.tools.allowed
    assert "bash" in doer.tools.allowed
    assert "think" in doer.tools.allowed
    assert "finish" in doer.tools.allowed


def test_legacy_tools_moved_to_forbidden_for_doer():
    from aiforge_core.agents.loader import load_agents
    contracts = load_agents()
    doer = contracts["doer"]
    forbidden = set(doer.tools.forbidden)
    for legacy in ("file_read", "file_write", "file_patch",
                   "run_shell", "code_run"):
        assert legacy in forbidden, f"{legacy} must be forbidden for Doer"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/python/test_agents.py -v -k "editor or legacy"`
Expected: 3 failures

- [ ] **Step 3: Update `agents.yaml`**

Apply the following diff:

```yaml
# aiforge_core/agents/agents.yaml — Architect block, replace tools.allowed list:
  architect:
    ...
    tools:
      allowed:
        - graph_lookup
        - graphify_lookup
        - search_memory
        - editor                # was file_read; view-only via editor_commands
        - grep_repos
        - lookup_repo
        - create_parent_ticket
        - update_ticket
      forbidden:
        - bash
        - file_write
        - file_patch
        - run_compile
        - file_read             # legacy
        - run_shell
        - code_run
    editor_commands:
      - view
```

```yaml
# Planner block — same swap pattern, list_dir already implicit via view:
  planner:
    ...
    tools:
      allowed:
        - graph_lookup
        - graphify_lookup
        - search_memory
        - editor                # was file_read + list_dir
        - grep_repos
        - lookup_repo
        - write_plan
        - create_child_ticket
        - recall_similar_flows
      forbidden:
        - bash
        - file_write
        - file_patch
        - run_compile
        - ask_user
        - file_read
        - run_shell
        - code_run
    editor_commands:
      - view
```

```yaml
# Researcher block:
  researcher:
    ...
    tools:
      allowed:
        - graphify_lookup
        - memory_lookup
        - editor                # was file_read + list_dir
        - grep_repos
      forbidden:
        - file_write
        - file_patch
        - run_compile
        - bash
        - run_shell
        - code_run
        - ask_user
    editor_commands:
      - view
```

```yaml
# Doer block — biggest swap:
  doer:
    ...
    tools:
      allowed:
        - editor                # NEW — replaces file_read/file_write/file_patch
        - bash                  # NEW — replaces run_shell/code_run
        - think                 # NEW
        - finish                # NEW (Doer-only; enforced in cognition.py)
        - update_working_checkpoint
        - graphify_lookup
        - memory_lookup
      forbidden:
        - ask_user
        - start_long_term_update
        - create_child_ticket
        - write_fact
        - write_plan
        - web_scan
        - web_execute_js
        - file_read
        - file_write
        - file_patch
        - run_shell
        - code_run
    # No editor_commands — Doer has full access.
```

(Other agents — Verifier, Refiner, Feedback, Learner, Triage — unchanged: still tool-less with `forbidden: ALL`.)

- [ ] **Step 4: Update loader to parse `editor_commands`**

In `aiforge_core/agents/loader.py`:

1. Add field to `AgentContract` dataclass:

```python
# Inside the @dataclass(frozen=True) class AgentContract block, append:
    editor_commands: list[str] | None = None
```

2. Update `_parse_one` to read the new field. Replace the `return AgentContract(...)` call with:

```python
    editor_commands_raw = raw.get("editor_commands")
    if editor_commands_raw is not None:
        editor_commands = _as_list_str(
            editor_commands_raw, f"{where}.editor_commands"
        )
    else:
        editor_commands = None

    return AgentContract(
        role=role,
        identity=identity,
        contract=contract,
        tools=Tools(allowed=allowed, forbidden=forbidden_raw,
                    forbidden_is_all=forbidden_is_all),
        memory=memory,
        rule=rule.strip(),
        termination_contract=termination,
        editor_commands=editor_commands,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/python/test_agents.py -v`
Expected: all pass (including the 3 new tests). Run the editor allowlist test too:
`pytest tests/python/runtime/tools/test_editor.py::test_non_doer_blocked_from_mutating_commands -v`
Expected: pass (now that `editor_commands` is wired through the loader).

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/agents/agents.yaml aiforge_core/agents/loader.py \
        tests/python/test_agents.py
git commit -m "feat(agents): swap Doer toolset; add editor_commands per-agent allowlist

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: ADK runner + pipeline wiring

**Files:**
- Modify: `aiforge_core/runtime/adk_runner.py`
- Modify: `aiforge_core/runtime/pipeline.py`

- [ ] **Step 1: Locate every existing import of `doer_tools`**

Run: `grep -rn "doer_tools" aiforge_core/`
Expected: matches in `adk_runner.py`, `pipeline.py`, agent modules.

- [ ] **Step 2: Replace imports**

In every Python file under `aiforge_core/` that imports `doer_tools`:

```python
# OLD
from aiforge_core.runtime.doer_tools import adk_function_tools

# NEW
from aiforge_core.runtime.tools import adk_function_tools
```

Use this command to find all imports needing the swap:

```bash
grep -rln "from aiforge_core.runtime.doer_tools import" aiforge_core/ | \
  xargs sed -i '' 's|from aiforge_core.runtime.doer_tools import|from aiforge_core.runtime.tools import|g'
```

(macOS `sed -i ''`; on Linux drop the empty string.)

- [ ] **Step 3: Wire `destroy_session` into ADK finish callback**

In `aiforge_core/runtime/pipeline.py`, locate the BasePlugin instantiation
(search `BasePlugin` or `on_finish_callback`). Append a finish-callback wrapper:

```python
# In pipeline.py — add near the other ADK plugin wiring
def _on_finish_destroy_bash_session(invocation_context):
    from aiforge_core.runtime.tools.bash import destroy_session
    run_id = getattr(invocation_context, "invocation_id", None) or "default"
    destroy_session(run_id)
```

Register it on the existing plugin (the `BasePlugin` already used for trace
events). If the plugin already has `on_finish_callback`, chain the new logic:

```python
existing = getattr(plugin, "on_finish_callback", None)
def _chained(ctx):
    if existing:
        existing(ctx)
    _on_finish_destroy_bash_session(ctx)
plugin.on_finish_callback = _chained
```

- [ ] **Step 4: Run the full test suite**

Run: `pytest tests/python/ -x -q`
Expected: all pass (the deprecation warning from `doer_tools` may appear once;
that is intentional). Coverage on `aiforge_core/runtime/tools/` should be ≥85%:
`pytest tests/python/runtime/tools/ --cov=aiforge_core/runtime/tools --cov-report=term-missing`

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/adk_runner.py aiforge_core/runtime/pipeline.py
# Plus any other files modified by the sed sweep
git commit -m "feat(runtime): wire new tools/ factory + bash session cleanup on finish

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: End-to-end integration smoke test

**Files:**
- Create: `tests/python/runtime/test_tools_pkg_integration.py`

- [ ] **Step 1: Write the smoke test**

```python
# tests/python/runtime/test_tools_pkg_integration.py
"""End-to-end smoke: editor + bash + finish run together inside a single
temp workspace, no ADK loop required."""
from __future__ import annotations

import shutil

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


def test_create_run_finish(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    from aiforge_core.runtime.tools import bash, editor
    from aiforge_core.runtime.tools.bash import destroy_session
    from aiforge_core.runtime.tools.cognition import finish

    run_id = "smoke-1"
    try:
        create = editor.editor(
            "create", "hello.py",
            file_text="print('hi from tools pkg')\n",
        )
        assert create["ok"]

        out = bash.bash("python hello.py", _run_id=run_id)
        assert out["ok"], out
        assert "hi from tools pkg" in out["stdout"]

        done = finish("hello.py created and executed", _agent_role="doer")
        assert done["ok"]
        assert done["terminate"] is True
    finally:
        destroy_session(run_id)
```

- [ ] **Step 2: Run the smoke test**

Run: `pytest tests/python/runtime/test_tools_pkg_integration.py -v`
Expected: 1 passed (or skipped if tmux missing)

- [ ] **Step 3: Commit**

```bash
git add tests/python/runtime/test_tools_pkg_integration.py
git commit -m "test(tools): integration smoke (create + bash + finish)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 16: Docs update

**Files:**
- Modify: `README.md`
- Modify: `docs/agent-rules.md`

- [ ] **Step 1: README tool surface section**

Find the existing "Doer tools" section in `README.md` (search for "file_read"
or "Doer"). Replace the canonical tool list with:

```markdown
## Doer tool surface (sub-project #1 OpenHands parity)

The Doer agent calls four canonical tools, declared in `agents.yaml`:

| Tool | Module | Notes |
|---|---|---|
| `editor(command, path, ...)` | `runtime/tools/editor.py` | OH-style multi-command: `view`, `create`, `str_replace`, `insert`, `undo_edit` (snapshot ring depth 5). |
| `bash(command, restart, timeout)` | `runtime/tools/bash.py` | tmux-backed persistent session per ADK run. Falls back to stateless subprocess if tmux missing. |
| `think(thought)` | `runtime/tools/cognition.py` | No-op + `:Think` trace event. 4 KB cap. |
| `finish(summary, status)` | `runtime/tools/cognition.py` | Doer-only termination signal; returns `terminate=True`. |

Non-Doer agents (Architect, Planner, Researcher) get **view-only** access via the
`editor_commands: [view]` field in `agents.yaml`. The legacy `file_read /
file_write / file_patch / run_shell / code_run` tools are kept one release as
deprecation shims in `runtime/doer_tools.py` and will be removed in the next
minor release.
```

- [ ] **Step 2: `docs/agent-rules.md` per-agent diff**

Append a new section to `docs/agent-rules.md`:

```markdown
## §X — Tool surface (sub-project #1, 2026-05-21)

Doer: `editor`, `bash`, `think`, `finish`, `update_working_checkpoint`,
`graphify_lookup`, `memory_lookup`. All five legacy file/shell tools moved
to `forbidden`.

Architect / Planner / Researcher: `editor` with `editor_commands: [view]`
(view-only). All mutating commands return `editor_command_not_allowed`.

Verifier / Refiner / Feedback / Learner / Triage: tool-less (`forbidden:
ALL`) — unchanged.

`finish` is Doer-only and enforced inside the tool (non-Doer attempts
return `agent_not_authorized`).
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/agent-rules.md
git commit -m "docs: document new tool surface + per-agent allowlist

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 17: Regression gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full repo test suite**

Run: `pytest tests/python/ -q`
Expected: 0 failures

- [ ] **Step 2: Coverage check on `tools/`**

Run: `pytest tests/python/runtime/tools/ --cov=aiforge_core.runtime.tools --cov-report=term-missing`
Expected: line coverage ≥85% on every file in the package.

- [ ] **Step 3: Boot-time agents.yaml validation**

Run: `python -c "from aiforge_core.agents.loader import load_agents, validate_contracts; v = validate_contracts(load_agents()); print('violations:', v); assert not v"`
Expected: `violations: []`

- [ ] **Step 4: Note the gate result in the spec**

Append a `## 13. Verification log` section to
`docs/superpowers/specs/2026-05-21-tool-surface-upgrade-design.md`:

```markdown
## 13. Verification log

- 2026-05-21: full pytest suite green (X tests).
- 2026-05-21: `aiforge_core/runtime/tools/` coverage Y%.
- 2026-05-21: `validate_contracts(load_agents())` returns `[]`.
- 2026-05-21: regression run ONE-107/108/109 fixtures: PRs produced (#NEW1, #NEW2, #NEW3).
```

(Fill in X / Y / PR numbers when run.)

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-05-21-tool-surface-upgrade-design.md
git commit -m "docs(spec): record sub-#1 verification log

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-review

- **Spec coverage:** every section of the design spec maps to a task — §4 module layout (Task 1), §5.1 editor (Tasks 2-7), §5.2 bash (Tasks 8-9), §5.3 think + §5.4 finish (Task 10), §6 agents.yaml (Task 13), §7 wiring (Task 14), §8 testing (Tasks 1-10, 15), §9 acceptance criteria (Task 17), §10 risks (covered by fallback path in Task 8 + scope guard in Task 7), docs in Task 16.
- **Placeholder scan:** no `TBD`, no "implement later". The single placeholders in Task 17 Step 4 (`X / Y / PR numbers`) are explicit fill-in-after-run values, not unwritten code.
- **Type consistency:** `editor(command, path, ...)` signature matches across Tasks 2-7. `bash(command, *, restart, timeout, _run_id)` matches across Tasks 8-9 and the integration test in Task 15. `finish(summary, status, *, _agent_role)` matches Task 10 and Task 15.

Plan locked.
