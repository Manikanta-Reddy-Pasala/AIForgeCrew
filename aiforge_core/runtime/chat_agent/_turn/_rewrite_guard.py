"""A whole-file write that would throw away the turn's own earlier edits is
refused.

sqlglot v2a, 44 minutes in: 19/25 hidden tests passing, then ``file_write``
of all of ``parser.py`` from what the model remembered of the original. The
trigger parser it had added an hour earlier was gone ("must have been added
in an earlier session"), and the run ended at 3/25. The discard guard covers
``rm`` and ``git checkout``; this is the same loss through a write tool.

Refused, when ``file_write`` / ``file_create`` replaces a file a tool of THIS
turn already wrote (``st.file_hashes``) and the new content no longer defines
a function or class the file gained since its committed version. Reworded or
restructured lines are not a loss: counting dropped lines refused a shopkit
run's clean-ups five times and cost it the turn.
A file new this turn (not in HEAD) is not guarded: rewriting one's own draft
is normal. The model can make the change with file_patch / multi_edit, or send
the same write again with ``"drop_edits": true`` when dropping them is what it
means (its own key: file_write's ``force`` also skips the syntax check). Off
switch: ``AIFORGE_CHAT_REWRITE_GUARD=0``.

The same holds for the repo's own code: a whole-file write of a committed
file that drops a function or class the file has is refused
(``rewrite_drops_existing_code``), whether or not the turn wrote it before.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import Counter

_WRITE_TOOLS = ("file_write", "file_create")
#: A top-level-ish definition line, in the common languages: the name is the
#: last group that matched. Python def/class; JS/TS function/class/const f =
#: (...) =>; Go func (incl. methods); Java/Kotlin/C#/Scala/Swift classes and
#: methods; Rust fn/struct/enum/trait/impl; Ruby def/class/module; PHP
#: function/class; C/C++ function definitions.
_P = {
    "py": r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)",
    "js_fn": r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+(\w+)",
    "js_cls": r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:class|interface|enum|type)\s+(\w+)",
    "js_arrow": r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>",
    "go": r"^func\s+(?:\([^)]*\)\s*)?(\w+)",
    "go_type": r"^type\s+(\w+)\s+(?:struct|interface)",
    "rs": r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:fn|struct|enum|trait|mod)\s+(\w+)",
    "rb": r"^\s*(?:def\s+(?:self\.)?|class\s+|module\s+)(\w+)",
    "jvm_type": (r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|override|"
                 r"virtual|sealed|open|suspend|async|synchronized|inline|data|partial)\s+)*"
                 r"(?:class|interface|enum|record|struct|object|fun)\s+(\w+)"),
    "jvm_method": (r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|"
                   r"override|virtual|synchronized|async)\s+)+[\w<>\[\],.?]+\s+(\w+)\s*\([^;]*$"),
    "php": r"^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*(?:function|class|interface|trait)\s+(\w+)",
    "c": r"^(?!\s)(?!(?:return|else|if|for|while|switch|do|case|goto)\b)[A-Za-z_][\w\*&:<>, ]*?[\s\*&]\**(\w+)\s*\([^;]*\)\s*(?:const\s*)?\{?\s*$",
}
#: Definition patterns by file extension; an unknown extension is not checked.
_BY_EXT = {
    ".py": ("py",), ".pyi": ("py",),
    **{e: ("js_fn", "js_cls", "js_arrow") for e in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")},
    ".go": ("go", "go_type"), ".rs": ("rs",), ".rb": ("rb",),
    **{e: ("jvm_type", "jvm_method") for e in (".java", ".kt", ".kts", ".cs", ".scala", ".swift")},
    ".php": ("php",),
    **{e: ("c",) for e in (".c", ".h", ".cc", ".cpp", ".hpp", ".cxx")},
}
_DEFS_BY_EXT = {e: [re.compile(_P[k]) for k in ks] for e, ks in _BY_EXT.items()}
_NOT_NAMES = frozenset({"if", "for", "while", "switch", "return", "catch", "else",
                        "sizeof", "new", "delete", "throw", "case", "do"})


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_REWRITE_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _committed(path: str) -> "str | None":
    """The file at HEAD of its repo; '' when it is new there; None outside git."""
    d = os.path.dirname(path)
    try:
        top = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if top.returncode != 0:
            return None
        rel = os.path.relpath(path, top.stdout.strip())
        shown = subprocess.run(["git", "-C", top.stdout.strip(), "show", f"HEAD:{rel}"],
                               capture_output=True, text=True, timeout=5,
                               errors="replace")
        return shown.stdout if shown.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return None


def _lines(text: str) -> Counter:
    return Counter(ln.strip() for ln in (text or "").splitlines() if len(ln.strip()) > 3)


def lost_lines(before: str, now: str, new: str) -> list:
    """Lines ``now`` gained over ``before`` that ``new`` no longer has."""
    gained = _lines(now) - _lines(before)
    return list((gained - _lines(new)).elements())


def _defs(text: str, path: str = "x.py") -> set:
    rxs = _DEFS_BY_EXT.get(os.path.splitext(path)[1].lower())
    if not rxs:
        return set()
    out = set()
    for ln in (text or "").splitlines():
        for rx in rxs:
            m = rx.match(ln)
            if m and m.group(1) not in _NOT_NAMES:
                out.add(m.group(1))
                break
    return out


def lost_defs(before: str, now: str, new: str, path: str = "x.py") -> list:
    """Functions/classes ``now`` gained over ``before`` that ``new`` lacks."""
    return sorted((_defs(now, path) - _defs(before, path)) - _defs(new, path))


def lost_existing_defs(before: str, now: str, new: str, path: str = "x.py") -> list:
    """Functions/classes the committed file has, still there now, that the
    whole-file write ``new`` no longer defines."""
    return sorted((_defs(before, path) & _defs(now, path)) - _defs(new, path))


def refusal(st, name: str, args: dict, cwd) -> "dict | None":
    if name not in _WRITE_TOOLS or not _on() or not isinstance(args, dict):
        return None
    if args.get("drop_edits") is True or str(args.get("drop_edits")).lower() == "true":
        return None
    path = str(args.get("path") or "")
    content = args.get("content")
    if not path or not isinstance(content, str):
        return None
    path = os.path.expanduser(path)
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd or ".", path))
    if not os.path.isfile(full):
        return None
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            now = fh.read()
    except OSError:
        return None
    before = _committed(full)
    if not before:
        return None                   # outside git, or new this turn: a draft
    # The repo's own code: boltons fr1a, 6 minutes in, wrote all of
    # strutils.py from memory to add one function; a dozen functions the
    # file had for years were gone, 10 old tests failed and the score fell
    # from 9/9 to 6/9 when the hour ran out.
    kept = lost_existing_defs(before, now, content, full)
    if kept:
        return {"ok": False, "error": "rewrite_drops_existing_code",
                "lost_defs": kept[:10],
                "hint": (f"this whole-file write would delete {len(kept)} function(s)/"
                         f"class(es) {path} already has: {', '.join(kept[:6])}"
                         f"{' ...' if len(kept) > 6 else ''}. To add or change code in an "
                         "existing file use file_patch or multi_edit. Send the same write "
                         "with \"drop_edits\": true only if deleting them is what the "
                         "request asks for.")}
    edited = {os.path.realpath(p) for p in (getattr(st, "file_hashes", None) or {})}
    if full not in edited:
        return None
    gone = lost_defs(before, now, content, full)
    if not gone:
        return None
    return {"ok": False, "error": "rewrite_drops_your_edits",
            "lost_defs": gone[:10],
            "hint": (f"this whole-file write would drop {', '.join(gone[:6])} — "
                     f"added to {path} earlier in this turn. Keep them in the new "
                     "content, make the change with file_patch or multi_edit, or send "
                     "the same write with \"drop_edits\": true if dropping them is intended.")}
