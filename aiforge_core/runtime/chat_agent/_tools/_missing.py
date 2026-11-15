"""What to say when a read names a file that does not exist.

A bare "not found: <path>" leaves a small model guessing. In a more-itertools
run it invented ``/home/ashishsadangi/.cache/mic/mic.py`` on its second call
— a path in no file, task or memory — and spent ~140 calls looking for it
(``ls`` of the user's cache, ``grep -rn /home``, ``/etc/passwd``) before its
first edit, and scored 0. The error now says where the work is: whether the
path is outside the repository, and the repository files with the same or a
close name. Bounded walk, no model call.
"""
from __future__ import annotations

import difflib
import os

_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist",
         "build", ".mypy_cache", ".pytest_cache", ".worktrees"}
_MAX_FILES = 6000


def _root(cwd: str) -> str:
    import subprocess
    try:
        p = subprocess.run(["git", "-C", cwd or ".", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=4)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.abspath(cwd or ".")


def _repo_files(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP and not d.startswith(".")]
        for f in files:
            out.append(os.path.relpath(os.path.join(dirpath, f), root))
            if len(out) >= _MAX_FILES:
                return out
    return out


def hint(path: str, cwd: str) -> dict:
    """Extra fields for a not-found result: ``hint`` and ``similar``."""
    try:
        root = _root(cwd)
        full = path if os.path.isabs(path) else os.path.join(cwd or ".", path)
        full = os.path.abspath(full)
        out: dict = {}
        if not (full == root or full.startswith(root.rstrip("/") + "/")):
            out["hint"] = (f"{path} is outside the repository ({root}) and does not "
                           "exist — the files of this task are inside the repository; "
                           "do not search the rest of the machine for it.")
        files = _repo_files(root)
        base = os.path.basename(full)
        same = [f for f in files if os.path.basename(f) == base]
        close = difflib.get_close_matches(base, [os.path.basename(f) for f in files],
                                          n=5, cutoff=0.75)
        similar = same + [f for f in files if os.path.basename(f) in close and f not in same]
        if similar:
            out["similar"] = similar[:5]
        elif "hint" not in out:
            out["hint"] = (f"no file named {base} in the repository ({root}); "
                           "use find or grep to locate what you need")
        return out
    except Exception:  # noqa: BLE001 — a hint must never break a read
        return {}
