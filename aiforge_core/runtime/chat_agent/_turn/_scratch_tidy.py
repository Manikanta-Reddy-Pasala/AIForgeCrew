"""At the end of a turn: the throwaway files the turn itself made leave the repo.

A real Rust run left 21 files in the repository root: test_partial_upper_bug.rs
… _bug13.rs, each with its compiled binary next to it, written to "check
something" and never removed. They show up in `git status`, in the next
turn's repo map, and in the user's commit.

Only files that (1) were untracked and absent when the turn started, (2) carry
a probe's name (test_bug*, debug*, repro*, scratch*, tmp*, probe*, analyze*,
check_*, verify* …) or are the compiled binary of such a source, and (3) are
not named in the request. They are MOVED to ``<config>/trash/<time>/`` (kept,
never deleted) and the turn says so. Real work — a new module, a test in the
repo's test folders — is never touched.
Off: ``AIFORGE_CHAT_SCRATCH_TIDY=0``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

#: A probe's file name: a probe word as a whole word anywhere in it
#: (test_partial_upper_bug13.rs, debug_parse.py, repro-x.sh, tmp_check.rs).
_PROBE = re.compile(r"(?:^|[_.-])(?:bug|debug|analy[sz]e|probe|repro|try|scratch|tmp|temp|"
                    r"check|quick|verify|trial|experiment|sanity)\d*(?:$|[_.-])", re.I)
#: Folders whose files are the repo's own tests or code: never tidied.
_KEEP_DIRS = re.compile(r"(?:^|/)(?:src|lib|pkg|internal|app|tests?|spec|__tests__)/")
_SOURCE_EXT = (".rs", ".py", ".go", ".c", ".cc", ".cpp", ".java", ".kt", ".js", ".ts", ".sh", ".rb")
_MAX = 200


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_SCRATCH_TIDY", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _git(cwd: str, *args) -> "str | None":
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", cwd, *args],
                           capture_output=True, text=True, timeout=15)
        return r.stdout if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _is_binary_of(path: str, stems: set) -> bool:
    """An extension-less executable named like a probe source made this turn."""
    name = os.path.basename(path)
    return "." not in name and name in stems and os.access(path, os.X_OK)


def candidates(top: str, before: set, goal: str) -> list:
    """Repo-relative paths of the probe files this turn made."""
    out = _git(top, "ls-files", "--others", "--exclude-standard", "--full-name", "-z")
    if out is None:
        return []
    new = [p for p in out.split("\0") if p and p not in before]
    low_goal = (goal or "").lower()
    probes = [p for p in new if _PROBE.search(os.path.basename(p))
              and not _KEEP_DIRS.search(p)
              and os.path.basename(p).lower() not in low_goal
              and os.path.splitext(os.path.basename(p))[0].lower() not in low_goal]
    stems = {os.path.splitext(os.path.basename(p))[0] for p in probes
             if p.endswith(_SOURCE_EXT)}
    picked = [p for p in probes if p.endswith(_SOURCE_EXT) or "." in os.path.basename(p)
              or _is_binary_of(os.path.join(top, p), stems)]
    return picked[:_MAX]


def tidy(st, cwd: str) -> "dict | None":
    """Move this turn's probe files out of the repo; the event to show, or None."""
    if not _on() or getattr(st, "readonly_mode", False):
        return None
    before = getattr(st, "example_untracked0", None)
    if before is None:
        return None                       # no start snapshot: nothing is known to be the turn's
    top = (_git(str(cwd), "rev-parse", "--show-toplevel") or "").strip()
    if not top:
        return None
    paths = candidates(top, set(before), str(getattr(st, "goal", "") or ""))
    if not paths:
        return None
    try:
        from aiforge_core.config.paths import config_dir
        dest = os.path.join(str(config_dir()), "trash", time.strftime("%Y%m%d-%H%M%S"))
    except Exception:  # noqa: BLE001
        return None
    moved = []
    for rel in paths:
        src = os.path.join(top, rel)
        try:
            os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
            shutil.move(src, os.path.join(dest, rel))
            moved.append(rel)
        except OSError:
            continue
    if not moved:
        return None
    shown = ", ".join(moved[:6]) + (f" and {len(moved) - 6} more" if len(moved) > 6 else "")
    return {"type": "thought", "role": "system",
            "text": f"🧹 moved {len(moved)} throwaway file(s) this turn made out of the repo "
                    f"({shown}) — kept in {dest}"}


__all__ = ["tidy", "candidates"]
