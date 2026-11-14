"""The working set, handed back after a condense.

A condense keeps the task, the board and the NAMES of the files the turn
edited, and summarises the rest. With a 125k window a long task condenses, and
the model then has its own edits only as file names: in tomlkit runs every
recent run that condensed scored 0-2 — it re-read whole files in 50-line
slices and never got back to where it was.

So after a condense the next message carries what the model needs to go on:
the DIFF of every file it changed (``git diff -U30`` against the work's base —
its own edits with 30 lines around each), new files' heads, and the tests the
regression watch last named as broken. Bounded (``AIFORGE_CHAT_WORKSET_CHARS``,
24000 ≈ 6k tokens) and most recently edited first; read from disk, so it is
the CURRENT state, not a stale copy. Off: ``AIFORGE_CHAT_CONDENSE_WORKSET=0``.
"""
from __future__ import annotations

import os
import subprocess


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_CONDENSE_WORKSET", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _cap() -> int:
    try:
        return max(2000, int(os.environ.get("AIFORGE_CHAT_WORKSET_CHARS", "24000")))
    except ValueError:
        return 24000


def _git(root: str, *args: str) -> "str | None":
    try:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def note(st, after: str = "the history was condensed") -> str:
    """The working-set note for the message after a condense or a context
    reset between board items ("" if none)."""
    if not _on():
        return ""
    cwd = str(getattr(st, "cwd", "") or "")
    edited = [p for p in (getattr(st, "file_hashes", {}) or {}) if os.path.isfile(p)]
    if not cwd or not edited:
        return ""
    from aiforge_core.runtime import ops_memory
    root = ops_memory.repo_root(cwd)
    if not root:
        return ""
    edited.sort(key=lambda p: os.path.getmtime(p), reverse=True)   # newest edit first
    base = None
    try:
        from ._contract_gate import work_base
        base = work_base(st)
    except Exception:  # noqa: BLE001
        base = None
    tracked = set((_git(root, "ls-files") or "").splitlines())
    cap, used, parts, left = _cap(), 0, [], []
    for full in edited:
        rel = os.path.relpath(full, root)
        if rel.startswith(".."):
            continue
        if rel in tracked:
            body = _git(root, "diff", "-U30", base or "HEAD", "--", rel) or ""
            if not body.strip():
                continue
            block = f"--- {rel} (your changes, git diff -U30) ---\n{body.rstrip()}"
        else:
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    head = "".join(fh.readlines()[:150])
            except OSError:
                continue
            block = f"--- {rel} (new file, first 150 lines) ---\n{head.rstrip()}"
        if used + len(block) > cap:
            left.append(rel)
            continue
        parts.append(block)
        used += len(block)
    broken = sorted(getattr(st, "regress_said", set()) or set())
    if not parts and not broken and not left:
        return ""
    out = [f"[harness: {after}. Your working set as it is on "
           "disk NOW — continue from here, do not re-read these files whole:]"]
    out.extend(parts)
    if left:
        out.append("(also edited, not shown for room: " + ", ".join(left[:12]) + ")")
    if broken:
        out.append("Tests the regression check last named as broken by your edits: "
                   + ", ".join(broken[:15]) + (" …" if len(broken) > 15 else ""))
    return "\n\n".join(out)
