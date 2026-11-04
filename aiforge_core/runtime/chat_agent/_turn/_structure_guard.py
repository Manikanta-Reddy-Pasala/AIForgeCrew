"""After an edit to a Python file: did it move or drop a function or class it
never named?

A valid-Python edit can still break the file. On click the model inserted a
``shell_complete`` method whose end swallowed the next method: ``invoke`` and
its helpers became the body of the new method. Every syntax and error check
passed; the tests failed far away, and the model spent 25 minutes diagnosing
"two shell_complete methods" in a file it had broken itself one step earlier.

Before an edit the target ``.py`` files are read (:func:`snapshot`); after it
(:func:`check`) the dotted names they define (``MultiCommand.invoke``) are
compared. A name the file had before and has no more is reported — "now
nested inside X" when the same leaf name is still defined elsewhere — unless
the edit's own old text names it (``def invoke`` in ``old_text`` means the
edit meant to touch it). A whole-file write only reports moves, never
removals: rewriting a file may drop anything on purpose. Off switch:
``AIFORGE_CHAT_STRUCTURE_CHECK=0``. Soft-fail throughout.
"""
from __future__ import annotations

import ast
import os

from aiforge_core.runtime.tools.mutating import writes_files

from ._progress import _full, _named_paths

_MAX_BYTES = 2_000_000
_MAX_SHOWN = 5
_WHOLE_FILE = frozenset({"file_write", "file_create", "write"})


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
    if not _on() or not writes_files(name, args if isinstance(args, dict) else {}):
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


def _old_texts(args) -> str:
    if not isinstance(args, dict):
        return ""
    parts = [args.get(k) for k in ("old_text", "old_str", "old")]
    for ed in args.get("edits") or ():
        if isinstance(ed, dict):
            parts += [ed.get(k) for k in ("old_text", "old_str", "old")]
    return "\n".join(p for p in parts if isinstance(p, str))


def lost_names(before: str, after: str, old_text: str, whole_file: bool) -> list:
    """``[(name, where it is now or None)]`` the edit moved or removed."""
    b, a = _defs(before), _defs(after)
    if b is None or a is None:
        return []
    out = []
    for q in b:
        if q in a:
            continue
        leaf = q.rsplit(".", 1)[-1]
        if f"def {leaf}" in old_text or f"class {leaf}" in old_text:
            continue
        parent = q.rsplit(".", 1)[0] if "." in q else ""
        if parent and parent not in a:
            continue                 # its class or function went: said for that one
        now = next((n for n in a if n.rsplit(".", 1)[-1] == leaf
                    and n not in b), None)
        if now is None and whole_file:
            continue
        out.append((q, (now, a[now]) if now else None))
    return out


def check(st, name, files, args):
    """Events + a note for the model when the edit just landed moved or
    removed a definition it never named."""
    pre = getattr(st, "pre_edit", None) or {}
    st.pre_edit = {}
    if not pre or not _on():
        return
    for shown, full in files:
        before = pre.get(full)
        if before is None:
            continue
        after = _read(full)
        if after is None or after == before:
            continue
        try:
            lost = lost_names(before, after, _old_texts(args), name in _WHOLE_FILE)
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
