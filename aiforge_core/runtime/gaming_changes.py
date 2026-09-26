"""What THIS turn / run added to the tree — for :mod:`gaming_check`.

Diffing against ``HEAD`` is not that: the user's own uncommitted and
untracked code shows up too, and a gaming hit on it ends in "Undo that" on
the user's file (or a pipeline forced to ``partial``). So a turn/run takes a
:func:`baseline` when it starts — the HEAD sha plus, for every file that was
ALREADY dirty or untracked then, a HASH of each of its lines (never the
content) — and :func:`repo_changes` reports only what changed since:

* a file clean at the baseline: its lines added since the baseline HEAD
  (``git diff <head>``), as before;
* a file dirty at the baseline: the lines whose hashes are not in its
  baseline line-hash sequence (a line diff on hashes), or nothing when it
  did not change;
* a dirty file whose hashes could not be taken (too big): never scanned — no
  finding is better than a false one on the user's code.

A baseline is taken synchronously but cheaply (``git status`` plus a hash of
the dirty files only, cached by path + mtime + size), lives in this process
keyed by the token the caller keeps, and is dropped by :func:`release` when
that run ends — a long pipeline run's baseline is never evicted under it.
A token whose baseline is missing or incomplete makes :func:`repo_changes`
report NOTHING (the gaming check is skipped): falling back to ``HEAD`` would
flag the user's own uncommitted code.
"""
from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
import subprocess
import threading
import uuid
from collections import OrderedDict

log = logging.getLogger("aiforge.gaming_changes")

_MAX_FILES = 200
_MAX_BYTES = 512 * 1024
#: Dirty files hashed per baseline; more and the baseline is incomplete.
_MAX_DIRTY = 2000
#: Live baselines kept at once. Only a leak (a caller that never released)
#: reaches it; the oldest goes, loudly.
_MAX_BASES = 1024
_DIGEST = 8

_BASES: "OrderedDict[str, dict]" = OrderedDict()
_LOCK = threading.Lock()
#: (path, mtime_ns, size) -> line hashes, so a re-baseline hashes only what
#: changed on disk.
_HASHES: "OrderedDict[tuple, bytes | None]" = OrderedDict()
_MAX_HASHES = 20000


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


def _line_hashes(text: str) -> list[bytes]:
    return [hashlib.blake2b(line.encode("utf-8", "replace"),
                            digest_size=_DIGEST).digest()
            for line in text.splitlines()]


def _hash_file(root: str, rel: str) -> bytes | None:
    """The file's line hashes, packed; None when unreadable or too big.
    Cached by (path, mtime, size): an unchanged file is never re-read."""
    full = os.path.join(root, rel)
    try:
        st = os.stat(full)
    except OSError:
        return None
    key = (full, st.st_mtime_ns, st.st_size)
    with _LOCK:
        if key in _HASHES:
            _HASHES.move_to_end(key)
            return _HASHES[key]
    text = _read(root, rel)
    packed = None if text is None else b"".join(_line_hashes(text))
    with _LOCK:
        _HASHES[key] = packed
        while len(_HASHES) > _MAX_HASHES:
            _HASHES.popitem(last=False)
    return packed


def _unpack(packed: bytes) -> list[bytes]:
    return [packed[i:i + _DIGEST] for i in range(0, len(packed), _DIGEST)]


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


def _take(cwd: str) -> dict:
    head = (_git(cwd, "rev-parse", "HEAD") or "").strip()
    dirty = _dirty_paths(cwd) if head else None
    if not head or dirty is None:
        return {}
    if len(dirty) > _MAX_DIRTY:
        return {"head": head, "files": {}, "complete": False}
    return {"head": head, "complete": True,
            "files": {rel: _hash_file(cwd, rel) for rel in dirty}}


def baseline(cwd: str, *, background: bool = False) -> str:
    """Snapshot what is ALREADY changed in ``cwd``; returns a token for
    :func:`repo_changes` ("" outside a git repository). Always synchronous
    (``background`` is accepted for old callers): it costs a ``git status``
    and a hash of the dirty files, and a baseline still being taken when the
    check ran was a race the check could lose. Pair it with :func:`release`."""
    del background
    if not cwd or not os.path.isdir(cwd):
        return ""
    box = _take(cwd)
    if not box:
        return ""
    token = uuid.uuid4().hex
    with _LOCK:
        _BASES[token] = box
        while len(_BASES) > _MAX_BASES:
            old, _ = _BASES.popitem(last=False)
            log.warning("gaming baseline %s dropped: %d live baselines — a "
                        "caller is not releasing them", old, _MAX_BASES)
    return token


def release(token: str | None) -> None:
    """The run that took ``token`` ended: drop its baseline."""
    if not token:
        return
    with _LOCK:
        _BASES.pop(token, None)


def _base(token: str) -> dict | None:
    with _LOCK:
        box = _BASES.get(token)
    if not box or not box.get("head") or not box.get("complete"):
        return None
    return box


def _since(before: bytes, now: str) -> set[int]:
    """Line numbers of ``now`` that are not in the ``before`` line hashes."""
    a, b = _unpack(before), _line_hashes(now)
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
    THIS turn's / run's lines) — and NOTHING when that snapshot is missing or
    incomplete; since HEAD only when no token is given at all."""
    b = None
    if base:
        b = _base(base)
        if b is None:
            log.info("gaming check skipped: baseline %s missing or "
                     "incomplete", base)
            return {}
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
            continue
        before = files[rel]
        now = _read(cwd, rel)
        if before is None or now is None:
            changes.pop(rel)         # unknown: never judged
            continue
        lines = _since(before, now)
        if lines:
            changes[rel] = lines
        else:
            changes.pop(rel)         # untouched by this run
    return changes


def _reset_for_tests() -> None:
    with _LOCK:
        _BASES.clear()
        _HASHES.clear()


__all__ = ["added_lines", "baseline", "release", "repo_changes"]
