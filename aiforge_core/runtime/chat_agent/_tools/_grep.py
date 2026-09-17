"""The find and grep tools: ripgrep when it is installed, a Python walk otherwise."""
from __future__ import annotations

import os
import subprocess

from .._shell import _workspace_root

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
              "__pycache__", ".next", "target", ".gradle", ".idea"}


def _matches_in(root: str, names, base: str, needle: str,
                suffix: str = "") -> list[str]:
    return [os.path.relpath(os.path.join(root, n), base) + suffix
            for n in names if not needle or needle in n.lower()]


def _t_find(args: dict, cwd: str) -> dict:
    """Fuzzy-locate files/dirs by partial name — so a vague/wrong folder
    name still resolves. args: name (substring, case-insensitive),
    kind ('dir'|'file'|'any'), limit."""
    base = str(_workspace_root() or cwd)
    needle = (args.get("name") or args.get("query") or "").lower()
    kind = (args.get("kind") or "any").lower()
    limit = int(args.get("limit", 60))
    hits: list[str] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if kind in ("dir", "any"):
            hits += _matches_in(root, dirs, base, needle, "/")
        if kind in ("file", "any"):
            hits += _matches_in(root, files, base, needle)
        if len(hits) >= limit:
            break
    return {"ok": True, "base": base, "matches": hits[:limit],
            "truncated": len(hits) > limit}


def _grep_target(base: str, want) -> tuple[str, str]:
    """``(dir_to_search, note)`` — tolerant of a wrong ``path``: fall back to
    the whole project and SAY so rather than returning nothing."""
    if not want:
        return base, ""
    cand = want if os.path.isabs(want) else os.path.join(base, want)
    if os.path.exists(cand):
        return cand, ""
    return base, f"path {want!r} not found — searched the whole project instead"


def _ripgrep(pattern: str, target: str, glob, limit: int) -> list[str] | None:
    """ripgrep's hits, or None when rg is absent or failed (caller falls back)."""
    import shutil as _sh
    rg = _sh.which("rg")
    if not rg:
        return None
    cmd = [rg, "-n", "-i", "--no-heading", "-m", str(limit)]
    for d in _SKIP_DIRS:
        cmd += ["-g", f"!{d}"]
    if glob:
        cmd += ["-g", glob]
    cmd += [pattern, target]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (p.stdout or "").splitlines()[:limit]
    except Exception:  # noqa: BLE001
        return None


def _grep_file(fp: str, rx, base: str, out: list[str], limit: int) -> bool:
    """Append this file's hits; True when the overall limit is reached."""
    try:
        with open(fp, encoding="utf-8", errors="ignore") as fh:
            for i, ln in enumerate(fh, 1):
                if rx.search(ln):
                    out.append(f"{os.path.relpath(fp, base)}:{i}:{ln.rstrip()[:200]}")
                    if len(out) >= limit:
                        return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _python_grep(pattern: str, target: str, base: str, glob,
                 limit: int) -> tuple[list[str], bool, str]:
    """``(matches, truncated, error)`` — the dependency-free fallback."""
    import fnmatch as _fn
    import re as _re2
    try:
        rx = _re2.compile(pattern, _re2.IGNORECASE)
    except _re2.error as e:
        return [], False, f"bad regex: {e}"
    out: list[str] = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            # fnmatch handles *.py, test_*, *_spec.ts etc. (the old
            # endswith(glob.lstrip("*")) only matched suffix globs).
            if glob and not _fn.fnmatch(f, glob):
                continue
            if _grep_file(os.path.join(root, f), rx, base, out, limit):
                return out, True, ""
    return out, False, ""


def _t_grep(args: dict, cwd: str) -> dict:
    """Recursive content search (ripgrep if present, else Python). Tolerant
    of a wrong ``path``: falls back to the working dir + says so. args:
    pattern (required), path (optional), glob (optional file filter)."""
    pattern = args.get("pattern") or args.get("query") or ""
    if not pattern:
        return {"ok": False, "error": "missing 'pattern'"}
    base = str(_workspace_root() or cwd)
    target, note = _grep_target(base, args.get("path"))
    limit = int(args.get("limit", 80))
    glob = args.get("glob")
    lines = _ripgrep(pattern, target, glob, limit)
    if lines is not None:
        return {"ok": True, "matches": lines, "note": note,
                "truncated": len(lines) >= limit}
    matches, truncated, error = _python_grep(pattern, target, base, glob, limit)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "matches": matches, "note": note,
            "truncated": truncated}
