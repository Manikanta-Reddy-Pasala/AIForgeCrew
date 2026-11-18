"""Deleting a file the project already had is the user's call.

Owner, 11-16: the agent may decide things on its own when the user does not
answer — "not a deletion one". On a real Rust run (approvals off) the model ran
`rm tests/emoji-test.txt && python3 scripts/unicode.py`, setting confirm_delete
itself: a 5,518-line tracked test-data file was gone, it could not be fetched
back (no web access), and three tests that read it failed (36/36 -> 33/36).

A shell command that would delete a file the repository TRACKS (it was there
before this turn; the turn's own new files and build output are not tracked)
is refused unless the request itself asks to delete or remove something —
whatever the approval mode, and whatever confirm_delete the model sets.
Off: ``AIFORGE_CHAT_KEEP_PROJECT_FILES=0``.
"""
from __future__ import annotations

import os
import re
import subprocess

_ASKS_DELETE = re.compile(
    r"\b(?:delete|remove|drop|get rid of|clean ?up|prune|purge|wipe|erase|rm)\b", re.I)


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_KEEP_PROJECT_FILES", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _top(cwd: str) -> "str | None":
    try:
        p = subprocess.run(["git", "-C", cwd or ".", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _tracked(top: str, paths: list) -> list:
    if not paths:
        return []
    rels = [os.path.relpath(p, top) for p in paths]
    try:
        p = subprocess.run(["git", "-C", top, "ls-files", "--", *rels],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    listed = set(p.stdout.splitlines()) if p.returncode == 0 else set()
    out = []
    for rel in rels:
        if rel in listed or any(x.startswith(rel.rstrip("/") + "/") for x in listed):
            out.append(rel)
    return out


#: Shared dependency caches: every project on the machine builds from them.
_CACHE_DIRS = re.compile(
    r"(?:~|\$HOME|\$\{HOME\}|/home/[^/\s]+|/Users/[^/\s]+|/root)?/"
    r"(?:\.m2(?:/repository)?|\.gradle(?:/caches)?|\.cargo/(?:registry|git)|\.npm|"
    r"\.cache/(?:pip|yarn|go-build)|go/pkg/mod|\.nuget/packages|\.ivy2(?:/cache)?|"
    r"\.pnpm-store|\.local/share/pnpm)(?=[/\s'\"]|$)")
_DELETES = re.compile(r"(?:^|[;&|(]\s*|\s)(?:sudo\s+)?(?:rm|rmdir|unlink|shred)\s|-delete\b")
_PURGES = re.compile(
    r"dependency:purge-local-repository|\bgo\s+clean\s+[^;&|]*-modcache|"
    r"\bnpm\s+cache\s+(?:clean|rm|clear)|\bpip3?\s+cache\s+(?:purge|remove)|"
    r"\byarn\s+cache\s+clean|\bpnpm\s+store\s+prune|\bnuget\s+locals\b[^;&|]*--?clear|"
    r"\bcargo\s+cache\b", re.I)
_ASKS_CACHE = re.compile(r"\b(?:cache|\.m2|local repository|purge)\b", re.I)


def _cache_refusal(st, cmd: str):
    """Refuse wiping a shared dependency cache. A real run, unable to find a
    class, ran `rm ~/.m2/.../commons-model.jar` and
    `mvn dependency:purge-local-repository`: the machine's Maven cache lost
    versions other projects pinned, and every offline build there broke."""
    if _ASKS_CACHE.search(str(getattr(st, "goal", "") or "")):
        return None
    wipes = bool(_DELETES.search(cmd) and _CACHE_DIRS.search(cmd))
    if not wipes and not _PURGES.search(cmd):
        return None
    return {"ok": False, "blocked": "shared_dependency_cache",
            "error": ("refused: this deletes or purges a shared dependency cache "
                      "(~/.m2, ~/.gradle, ~/.cargo, ~/.npm, pip, go, nuget …) that every "
                      "project on this machine builds from — other projects pin versions "
                      "that cannot be fetched again offline. Leave it alone: read the jar "
                      "or sources you need (unzip -l, a sources jar), or ask the user if "
                      "the cache really has to be cleared.")}


def refusal(st, name: str, args: dict, cwd: str = ""):
    """The result to return INSTEAD of running this call, or None."""
    if not _on():
        return None
    try:
        from aiforge_core.runtime.shell_writes import SHELL_TOOLS, command_of
        if name in SHELL_TOOLS:
            cache = _cache_refusal(st, command_of(args or {}) or "")
            if cache:
                return cache
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime.shell_writes import SHELL_TOOLS, command_of
        from aiforge_core.runtime.tools import delete_guard
        if name not in SHELL_TOOLS:
            return None
        if _ASKS_DELETE.search(str(getattr(st, "goal", "") or "")):
            return None
        cmd = command_of(args or {}) or ""
        top = _top(str(getattr(st, "cwd", "") or cwd or "."))
        if not top:
            return None
        hit = _tracked(top, delete_guard.inside_targets(cmd, top))
        if not hit:
            return None
        return {"ok": False, "blocked": "delete_project_file", "files": hit[:6],
                "error": ("refused: this deletes files the project already has ("
                          + ", ".join(hit[:6]) + "), and the request does not ask to "
                          "delete anything — that is the user's call. Leave them in place: "
                          "change the code instead, or move a file aside with `git mv` / "
                          "a copy if you only need it out of the way. Ask the user if it "
                          "must go.")}
    except Exception:  # noqa: BLE001 — a guard never breaks a step on its own error
        return None
