"""What THIS turn / run added to the tree — for :mod:`gaming_check`.

Diffing against ``HEAD`` is not that: the user's own uncommitted and
untracked code shows up too, and a gaming hit on it ends in "Undo that" on
the user's file (or a pipeline forced to ``partial``). So a turn/run takes a
:func:`baseline` when it starts — the HEAD sha plus the content of every
source file that was ALREADY dirty or untracked then — and
:func:`repo_changes` reports only what changed since:

* a file clean at the baseline: its lines added since the baseline HEAD
  (``git diff <head>``), as before;
* a file dirty at the baseline: the lines that differ from its baseline
  content (a line diff), or nothing when it did not change;
* a dirty file whose baseline content could not be kept (too big): never
  scanned — no finding is better than a false one on the user's code.

The baseline lives in this process (keyed by a token the caller keeps), so no
file content lands in pipeline state or trajectories.
"""
from __future__ import annotations

import difflib
import os
import re
import subprocess
import threading
import uuid
from collections import OrderedDict

_MAX_FILES = 200
_MAX_BYTES = 512 * 1024
_MAX_BASES = 32
_BASE_WAIT_S = 10.0

_BASES: "OrderedDict[str, dict]" = OrderedDict()
_LOCK = threading.Lock()


def _git(cwd: str, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "--no-optional-locks", *args], cwd=cwd,
                             capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace")


def _read(root: str, rel: str) -> str | None:
    try:
        full = os.path.join(root, rel)
        if os.path.getsize(full) > _MAX_BYTES:
            return None
        with open(full, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines(diff: str) -> dict[str, set[int]]:
    """``{path: {new line numbers added}}`` from a unified diff."""
    out: dict[str, set[int]] = {}
    path, lineno = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            continue
        m = _HUNK.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if path is None or raw.startswith(("--- ", "diff ", "index ")):
            continue
        if raw.startswith("+"):
            out.setdefault(path, set()).add(lineno)
            lineno += 1
        elif raw.startswith(" "):
            lineno += 1
    return out


def _dirty_paths(cwd: str) -> list[str] | None:
    """Paths modified, added or untracked now (porcelain: repo-relative)."""
    raw = _git(cwd, "status", "--porcelain", "-z", "--untracked-files=all")
    if raw is None:
        return None
    out: list[str] = []
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1                       # the rename's source path follows
        out.append(path)
    return out


def _take(cwd: str, box: dict) -> None:
    try:
        head = (_git(cwd, "rev-parse", "HEAD") or "").strip()
        dirty = _dirty_paths(cwd) if head else None
        if not head or dirty is None:
            return
        files: dict[str, str | None] = {}
        for rel in dirty[:_MAX_FILES * 5]:
            files[rel] = _read(cwd, rel)
        box.update(head=head, files=files,
                   overflow=len(dirty) > _MAX_FILES * 5)
    finally:
        box["ready"].set()


def baseline(cwd: str, *, background: bool = False) -> str:
    """Snapshot what is ALREADY changed in ``cwd``; returns a token for
    :func:`repo_changes` ("" outside a git repository). ``background=True``
    takes it on a thread (a chat turn does not wait for it)."""
    if not cwd or not os.path.isdir(cwd):
        return ""
    token = uuid.uuid4().hex
    box: dict = {"ready": threading.Event()}
    with _LOCK:
        _BASES[token] = box
        while len(_BASES) > _MAX_BASES:
            _BASES.popitem(last=False)
    if background:
        threading.Thread(target=_take, args=(cwd, box), daemon=True,
                         name="aiforge-gaming-base").start()
    else:
        _take(cwd, box)
    return token


def _base(token: str | None) -> dict | None:
    if not token:
        return None
    with _LOCK:
        box = _BASES.get(token)
    if box is None:
        return None
    box["ready"].wait(_BASE_WAIT_S)
    return box if box.get("head") else None


def _since(before: str, now: str) -> set[int]:
    """Line numbers of ``now`` that are not in ``before``."""
    a, b = before.splitlines(), now.splitlines()
    out: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            out.update(range(j1 + 1, j2 + 1))
    return out


def repo_changes(cwd: str, base: str | None = None,
                 keep=lambda p: True) -> dict[str, set[int] | None]:
    """Added lines per changed file that ``keep`` accepts; ``None`` = the
    whole file is new. Since the ``base`` token's snapshot when given (only
    THIS turn's / run's lines), else since HEAD."""
    b = _base(base)
    ref = b["head"] if b else "HEAD"
    diff = _git(cwd, "diff", ref, "-U0", "--no-ext-diff", "--no-color")
    if diff is None:
        return {}
    changes: dict[str, set[int] | None] = dict(added_lines(diff))
    listing = _git(cwd, "ls-files", "--others", "--exclude-standard") or ""
    for rel in listing.splitlines()[:_MAX_FILES]:
        changes.setdefault(rel.strip(), None)
    changes = {p: v for p, v in changes.items() if keep(p)}
    if b is None:
        return changes
    files = b["files"]
    for rel in list(changes):
        if rel not in files:
            if b.get("overflow"):
                changes.pop(rel)     # cannot tell the user's from ours
            continue
        before = files[rel]
        now = _read(cwd, rel)
        if before is None or now is None or now == before:
            changes.pop(rel)         # unknown, or untouched by this run
            continue
        lines = _since(before, now)
        if lines:
            changes[rel] = lines
        else:
            changes.pop(rel)
    return changes


def _reset_for_tests() -> None:
    with _LOCK:
        _BASES.clear()


__all__ = ["added_lines", "baseline", "repo_changes"]
