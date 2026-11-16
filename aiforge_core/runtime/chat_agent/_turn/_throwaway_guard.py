"""Throwaway programs written to "try things" — one note after the third.

The prompt already says "Do not write throwaway scripts to try things". On a
real Rust job (semver) the model still wrote 30+ probe programs
(/tmp/test_bug.rs, examples/debug_less_than.rs, examples/analyze_ge3.rs …)
and ran each: 86 write-and-run cycles, 237 model calls, 40 minutes, where
OpenCode with the same model took 4. After the third such file in a turn the
agent is told once to put the case in the repo's own test and run that;
each further one counts toward the stuck limit (no extension while it goes
on). The note alone did not hold: the next run wrote 73 more (/tmp/test1.rs
… /tmp/test50.rs) after it. So once the note has been given, a further
throwaway SOURCE file is refused before it is written (:func:`refusal`).
Off: ``AIFORGE_CHAT_THROWAWAY_GUARD=0``.
"""
from __future__ import annotations

import os
import re

_LIMIT = 3
_NAME = re.compile(r"^(?:test_?bug|debug|analy[sz]e|probe|repro|try|scratch|tmp|check_|quick)",
                   re.I)
_SHELL_WRITE = re.compile(r"(?:cat|tee)\s+(?:-a\s+)?>?\s*>?\s*['\"]?([^\s'\"<|;&]+\.\w{1,5})")
_WRITE_TOOLS = ("file_write", "write_file", "create_file", "file_create")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_THROWAWAY_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _throwaway(path: str, root: str) -> bool:
    """A probe: written outside the repository (e.g. /tmp while the repo is
    elsewhere), or named like one (test_bug, debug_*, analyze_*, probe*…)."""
    p = path.replace("\\", "/")
    if root and os.path.isabs(p):
        r = os.path.realpath(root).rstrip("/") + "/"
        if not os.path.realpath(p).startswith(r):
            return True
    return bool(_NAME.search(os.path.basename(p)))


def _written(name: str, args: dict) -> list:
    if name in _WRITE_TOOLS:
        path = str(args.get("path") or args.get("file") or "")
        return [path] if path else []
    if name in ("run_command", "bash", "shell", "run_shell"):
        cmd = str(args.get("cmd") or args.get("command") or "")
        return [m.group(1) for m in _SHELL_WRITE.finditer(cmd) if ">" in cmd or "tee" in cmd]
    return []


_SOURCE = re.compile(r"\.(?:rs|py|java|kt|scala|c|cc|cpp|cxx|h|hpp|go|js|ts|rb|cs|swift)$", re.I)


def refusal(st, name: str, args: dict, cwd: str = ""):
    """The result to return INSTEAD of running this call, or None. Only after
    the note was given, and only for a new throwaway source file."""
    if not _on() or not getattr(st, "probe_said", False):
        return None
    try:
        root = str(getattr(st, "cwd", "") or cwd or "")
        new = [p for p in _written(name, args or {})
               if _SOURCE.search(p) and _throwaway(p, root)]
        if not new:
            return None
        from ._limits import note_spin
        note_spin(st)
        return {"ok": False, "blocked": "throwaway_program",
                "error": (f"refused: {os.path.basename(new[0])} would be another throwaway "
                          "program. Put the case into the repository's own test (the "
                          "existing test file for this code) and run the project's test "
                          "command instead.")}
    except Exception:  # noqa: BLE001
        return None


#: Whole-file writes of one file in a turn before each further one is spin.
_REWRITES = 3


def _rewrite_spin(st, name: str, args: dict) -> None:
    """A file rewritten whole again and again is not progress: a real
    Java service run wrote the same two files 72 times with file_write,
    each one a "new edit", and was given its extension (85+ minutes). From
    the 4th whole-file write of one file, each counts toward the stuck limit
    (no extension while it goes on). file_patch / multi_edit are not counted."""
    if name not in _WRITE_TOOLS:
        return
    path = str((args or {}).get("path") or (args or {}).get("file") or "")
    if not path:
        return
    counts = st.__dict__.setdefault("rewrites", {}) if hasattr(st, "__dict__") else {}
    counts[path] = counts.get(path, 0) + 1
    if counts[path] > _REWRITES:
        from ._limits import note_spin
        note_spin(st)


def check(st, name: str, args: dict, cwd: str = "") -> None:
    if not _on():
        return
    try:
        _rewrite_spin(st, name, args or {})
    except Exception:  # noqa: BLE001
        pass
    try:
        root = str(getattr(st, "cwd", "") or cwd or "")
        new = [p for p in _written(name, args or {}) if _throwaway(p, root)]
        if not new:
            return
        # Every write counts, not every new name: a C++ run rewrote the same
        # /tmp/test_unicode.cpp 13 times and never reached three names.
        seen = st.__dict__.setdefault("probe_files", [])
        seen.extend(new)
        if len(seen) < _LIMIT:
            return
        from ._limits import note_spin
        if getattr(st, "probe_said", False):
            note_spin(st)
            return
        st.probe_said = True
        note_spin(st)
        from ._notes import hold
        shown = ", ".join(dict.fromkeys(os.path.basename(p) for p in seen[-6:]))
        hold(st, "[harness — not the user] You have written "
                 f"{len(seen)} throwaway programs to try things ({shown}). Stop doing that: "
                 "put the case you are checking into the repository's own test (the "
                 "existing test file for this code, or a new test where the project keeps "
                 "its tests) and run the project's test command. One test that fails "
                 "first and passes after the fix is all the evidence needed.")
    except Exception:  # noqa: BLE001 — a note must never break a step
        return
