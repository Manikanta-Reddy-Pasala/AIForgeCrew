"""After an edit to a Python file: did it move or drop a function or class it
never named?

A valid-Python edit can still break the file. On click the model inserted a
``shell_complete`` method whose end swallowed the next method: ``invoke`` and
its helpers became the body of the new method. Every syntax and error check
passed; the tests failed far away, and the model spent 25 minutes diagnosing
"two shell_complete methods" in a file it had broken itself one step earlier.

Before an edit the target ``.py`` files are read (:func:`snapshot`); after it
(:func:`check`) the dotted names they define (``MultiCommand.invoke``) are
compared. A name now nested somewhere else is always reported (an insert
anchored on ``def invoke`` re-emits that line at the wrong indent: the old
text names it, the move is still the bug). A name gone entirely is reported
unless that file's old text defines it (``def invoke`` there means the edit
meant to remove it). Whole-file writes, undo, rename and format calls are
not checked: they change structure on purpose. Off switch:
``AIFORGE_CHAT_STRUCTURE_CHECK=0``. Soft-fail throughout.
"""
from __future__ import annotations

import ast
import os
import re

from aiforge_core.runtime.tools.mutating import editor_command, writes_files

from ._progress import _full, _named_paths

_MAX_BYTES = 2_000_000
_MAX_SHOWN = 5
#: Calls that restructure on purpose: never checked.
_SKIP = frozenset({"file_write", "file_create", "write", "rename_symbol", "format"})


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_STRUCTURE_CHECK", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _read(path: str) -> "str | None":
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def snapshot(st, name, args, cwd) -> None:
    """Keep the ``.py`` files this call is about to write, as they are now."""
    st.pre_edit = {}
    args = args if isinstance(args, dict) else {}
    if (not _on() or name in _SKIP or not writes_files(name, args)
            or (name == "editor" and editor_command(args) in ("create", "undo_edit"))):
        return
    try:
        for p in dict.fromkeys(_named_paths(args, None) or ()):
            full = _full(p, cwd)
            if full.endswith(".py"):
                body = _read(full)
                if body is not None:
                    st.pre_edit[full] = body
    except Exception:  # noqa: BLE001
        st.pre_edit = {}


def _defs(src: str) -> "dict | None":
    """``{"Cls.meth": line}`` for every def/class, nested ones dotted."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    out: dict = {}

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}{ch.name}"
                out.setdefault(q, ch.lineno)
                walk(ch, q + ".")
            elif not isinstance(ch, (ast.Lambda,)):
                walk(ch, prefix)
    walk(tree, "")
    return out


def _old_texts(args, full: str, cwd: str) -> str:
    """The old text of the edits to the file ``full`` only."""
    if not isinstance(args, dict):
        return ""
    top = args.get("path") or args.get("file_path") or args.get("file") or ""
    parts = []
    for ed in ([args] + [e for e in (args.get("edits") or ()) if isinstance(e, dict)]):
        where = ed.get("path") or ed.get("file_path") or top
        if where and _full(str(where), cwd) != full:
            continue
        parts += [ed.get(k) for k in ("old_text", "old_str", "old")]
    return "\n".join(p for p in parts if isinstance(p, str))


def _names_word(text: str, leaf: str) -> bool:
    return re.search(rf"\b{re.escape(leaf)}\b", text) is not None


def lost_names(before: str, after: str, old_text: str) -> list:
    """``[(name, (where it is now, line) or None)]`` the edit moved or removed.

    A move DEEPER (``Multi.invoke`` → ``Multi.shell_complete.invoke``) is the
    swallowed-code shape and is reported even when the old text names it; a
    move up or sideways that the old text names is a refactor. A name gone
    entirely is excused when the old text names it at all (a rename may patch
    only ``parse(self`` → ``parse_args(self``)."""
    b, a = _defs(before), _defs(after)
    if b is None or a is None:
        return []
    out = []
    used: set = set()
    for q in b:
        if q in a:
            continue
        leaf = q.rsplit(".", 1)[-1]
        parent = q.rsplit(".", 1)[0] if "." in q else ""
        if parent and parent not in a:
            continue                 # its class or function went: said for that one
        named = _names_word(old_text, leaf)
        cands = [n for n in a if n.rsplit(".", 1)[-1] == leaf and n not in b
                 and n not in used]
        cands.sort(key=lambda n: (not n.startswith(parent + ".") if parent else False,
                                  a[n]))
        now = cands[0] if cands else None
        if now is None:
            if named:
                continue             # removed or renamed on purpose
            out.append((q, None))
            continue
        if named and now.count(".") <= q.count("."):
            continue                 # moved up or sideways on purpose
        used.add(now)
        out.append((q, (now, a[now])))
    return out


def check(st, name, files, args, cwd=""):
    """Events + a note for the model when the edit just landed moved or
    removed a definition it never named."""
    pre = getattr(st, "pre_edit", None) or {}
    st.pre_edit = {}
    if not pre or not _on():
        return
    for shown, full in files:
        before = pre.pop(full, None)       # one file named two ways: checked once
        if before is None:
            continue
        after = _read(full)
        if after is None or after == before:
            continue
        try:
            lost = lost_names(before, after, _old_texts(args, full, cwd))
        except Exception:  # noqa: BLE001
            continue
        if not lost:
            continue
        said = []
        for q, now in lost[:_MAX_SHOWN]:
            if now:
                outer = now[0].rsplit(".", 1)[0] if "." in now[0] else "module level"
                said.append(f"`{q}` is now `{now[0]}` (line {now[1]}) — nested inside "
                            f"`{outer}`")
            else:
                said.append(f"`{q}` is gone from the file")
        more = f" (+{len(lost) - _MAX_SHOWN} more)" if len(lost) > _MAX_SHOWN else ""
        yield {"type": "thought", "role": "system",
               "text": f"⚠ edit to {shown} moved code it did not name: {said[0]}{more}"}
        st.convo.append({"role": "user", "content":
            "[automated structure check — not the user] Your last edit to "
            f"{shown} changed more than its old text covered:\n- "
            + "\n- ".join(said) + more + "\nYour new text's end or indentation "
            "most likely swallowed the code after it. Fix this edit now — "
            "`git diff` on the file shows exactly what changed — before "
            "anything else; do not debug the tests first."})


__all__ = ["check", "lost_names", "snapshot"]
