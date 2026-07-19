# P2P Shared Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replicate the markdown memory tree across AIForgeCrew instances as a full mesh of pull-only peers, with no coordinator for replication and a failure-tolerant lease for compaction.

**Architecture:** Markdown is the source of truth and `memory.db` is a local derived index, so syncing means replicating text files. Immutable records (captures, briefs) merge by union on a content hash. Mutable records (OKF nodes, tombstones, the lease) carry `(origin, key, rev, updated_by)` and merge by last-writer-wins on a counter, never a clock. Each peer serves two read-only HTTP endpoints and pulls from every other peer on a timer.

**Tech Stack:** Python 3.12, FastAPI (already mounted), httpx, stdlib `socket` for SSDP, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-19-p2p-shared-memory-design.md`

---

## Deviations from the spec (decided during planning)

- **`peers.yaml` → `peers.json`.** PyYAML is not a root dependency (only transitive via the
  `aiforge-memory` package) and every config file in this repo is JSON written through the
  `aiforge_core/config/integrations.py:15` idiom. Matching the house pattern.
- **No new auth code.** `aiforge_core/api/api.py:581` already enforces `AIFORGE_API_TOKEN` as
  bearer auth on every path beginning `/api/`. Placing the sync routes there satisfies the
  spec's token requirement with zero new code.
- **Tombstones and the lease are `.json`, not `.md`.** They carry no prose, and keeping them
  out of `*.md` means the existing OKF readers never see them.

## Conventions to follow (from the existing codebase)

- `from __future__ import annotations` at the top of every module.
- Shallow type hints (`-> dict`, `-> list[dict]`, `-> Path`). Domain layers pass **dicts**, not
  models. Pydantic only for FastAPI request bodies.
- Heavy or optional imports go **inside functions** (`import yaml`, `aiforge_core.memory.*`).
- Module logger: `logging.getLogger("aiforge.sync")`. Lazy `%s` formatting, never f-strings.
- Soft-fail is the default: `except Exception:  # noqa: BLE001` with a comment saying *why*
  swallowing is correct. Hard raises only for security guards and API 4xx.
- Writes that must not corrupt use temp file + `os.replace`.
- End public modules with `__all__`.
- ruff: `line-length = 100`, target `py311`, `select = ["E","F","I","UP","B","SIM"]`.
- Run tests with `.venv/bin/pytest`. Tests set env with `monkeypatch` **before** importing the
  module under test; there are no shared fixtures.

## File structure

**New package — `aiforge_core/memory/sync/`**

| File | Responsibility |
|---|---|
| `__init__.py` | Public re-exports only |
| `_io.py` | The only place touching disk primitives: tree root, hashing, atomic write, JSON |
| `paths.py` | The only place that knows the on-disk layout: where an identity lives |
| `identity.py` | This peer's slug; the `origin`/`rev`/`updated_by` stamp on a local write |
| `manifest.py` | Build the local manifest from the memory tree; resolve a hash back to a path |
| `merge.py` | **Pure.** Two manifests in, a want/conflict decision set out. No I/O |
| `peers.py` | `peers.json` load/save, roster gossip merge, candidate quarantine |
| `transport.py` | HTTP only. Fetch a manifest or blob from one peer. Never touches a path |
| `apply.py` | Disk only. Verify a blob, place it, keep sidecars. Never imports httpx |
| `tombstone.py` | Express a local delete as a record the mesh can merge |
| `lease.py` | Claim / renew / check the compaction lease |
| `discovery_ssdp.py` | Multicast announce and search on the local segment |
| `loop.py` | Scheduler: run one cycle across all approved peers; CLI entry |

**Separation of concerns.** `transport.py` and `apply.py` were a single `client.py` in an
earlier draft. They are split because "talk HTTP to a peer" and "decide where a file goes on
disk" fail differently, are tested differently, and change for different reasons. Nothing in
`apply.py` imports httpx; nothing in `transport.py` touches a path.

**DRY.** Every module needs the memory-tree root, a content hash, and a crash-safe write —
those live once, in `_io.py`. Every module that resolves an identity to a file needs the layout
rule — it lives once, in `paths.py`. No module reimplements either, and no module hand-rolls
temp-file-plus-`os.replace`.

**KISS.** `merge.py` stays pure so the only intricate logic is testable with two lists and no
fixtures. Every other module stays thin enough to read on one screen; none should approach the
500-line house cap.

**New route — `aiforge_core/api/routes/sync.py`** (mounted in `aiforge_core/api/api.py`)

**New tests — `tests/python/memory/sync/`**: `test_io.py`, `test_paths.py`, `test_manifest.py`,
`test_merge.py`, `test_peers.py`, `test_apply.py`, `test_lease.py`, `test_tombstone.py`,
`test_ssdp.py`, `test_sync_routes.py`, `test_two_peer.py`

## Data shapes

Manifest entry, class A (immutable):

```python
{"path": "captures/foo-20260719-ab12cd.md", "hash": "<sha256 hex of file bytes>", "cls": "A"}
```

Manifest entry, class B (mutable):

```python
{"path": "okf/global/learnings/L-07.md", "hash": "<sha256 hex>", "cls": "B",
 "origin": "nuc", "key": "L-07", "rev": 47, "updated_by": "ms"}
```

Note the manifest `hash` is **sha256 of the file bytes**, used for integrity and for class A
identity. It is unrelated to the existing `sha1(title+text)[:6]` filename digest
(`aiforge_core/memory/md_store/_ingest.py:72`), which is a dedupe device inside the filename.

On-disk additions under `memory_dir()`:

```
okf/peers/<origin>/<key>.md      nodes minted by another peer (see below)
okf/.tomb/<origin>/<key>.json    {"origin","key","rev","updated_by","tomb":true}
okf/.lease.json                  {"origin":"", "key":"__lease__", "rev", "updated_by",
                                  "holder", "expires_at"}
okf/**/<id>.conflict.md          local-only loser text; never synced
```

**Invariant:** for any `(origin, key)` at most one of the node file or its tombstone exists.

**Target-path rule.** OKF ids are per-scope counters, so `(nuc, O-01)` and `(ms, O-01)` are
different objects that both render to `O-01.md` (`okf/store.py:115`). The receiver therefore
computes the local path itself and treats the remote's `path` as a hint only: an identity
already held is updated wherever it lives, and anything new from another peer lands under
`okf/peers/<origin>/`. Every peer derives the same answer, so the layout converges. Class A is
exempt — capture filenames embed a content digest and are already globally unique.

---

# Phase 1 — Manifest and the version stamp

## Task 1: Sync package skeleton and class A manifest

**Files:**
- Create: `aiforge_core/memory/sync/__init__.py`
- Create: `aiforge_core/memory/sync/manifest.py`
- Test: `tests/python/memory/sync/__init__.py`
- Test: `tests/python/memory/sync/test_manifest.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/__init__.py` as an empty file, then
`tests/python/memory/sync/test_manifest.py`:

```python
"""Manifest construction from the markdown memory tree."""
from __future__ import annotations

import hashlib


def _seed_capture(root, name: str, text: str) -> None:
    d = root / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def test_class_a_entries_carry_sha256_of_file_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    root = tmp_path / "md"
    _seed_capture(root, "a-20260719-aaaaaa.md", "hello")

    entries = manifest.build()
    assert len(entries) == 1
    e = entries[0]
    assert e["path"] == "captures/a-20260719-aaaaaa.md"
    assert e["cls"] == "A"
    assert e["hash"] == hashlib.sha256(b"hello").hexdigest()


def test_briefs_are_class_a_too(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    root = tmp_path / "md"
    (root / "compacted").mkdir(parents=True, exist_ok=True)
    (root / "compacted" / "compacted-repo.md").write_text("brief", encoding="utf-8")

    paths = {e["path"] for e in manifest.build()}
    assert paths == {"compacted/compacted-repo.md"}


def test_path_for_hash_only_resolves_advertised_files(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    root = tmp_path / "md"
    _seed_capture(root, "a-20260719-aaaaaa.md", "hello")
    (root / "secret.txt").write_text("nope", encoding="utf-8")

    good = hashlib.sha256(b"hello").hexdigest()
    assert manifest.path_for_hash(good) is not None
    assert manifest.path_for_hash(hashlib.sha256(b"nope").hexdigest()) is None
    assert manifest.path_for_hash("../../etc/passwd") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_manifest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aiforge_core.memory.sync'`

- [ ] **Step 3: Write minimal implementation**

Create `aiforge_core/memory/sync/__init__.py`:

```python
"""Peer-to-peer replication of the markdown memory tree.

Markdown is the source of truth and ``memory.db`` is a local derived index, so
syncing means replicating text files. See
``docs/superpowers/specs/2026-07-19-p2p-shared-memory-design.md``.
"""
from __future__ import annotations

__all__ = ["manifest", "merge"]
```

Create `aiforge_core/memory/sync/manifest.py`:

```python
"""Build the local sync manifest from the markdown memory tree.

Class A (``captures/``, ``compacted/``) is immutable and merges by union on a
content hash. Class B (OKF nodes, tombstones, the compaction lease) is mutable
and carries ``(origin, key, rev, updated_by)`` so two versions can be ordered
without consulting a clock.

The manifest ``hash`` is sha256 of the file bytes. It is unrelated to the
``sha1(title+text)[:6]`` digest embedded in capture filenames, which is a
dedupe device rather than an integrity check.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

_log = logging.getLogger("aiforge.sync")


def _root() -> Path:
    from aiforge_core.memory.md_store import memory_dir

    return memory_dir()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _class_a(root: Path) -> list[dict]:
    out: list[dict] = []
    for sub in ("captures", "compacted"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                out.append({"path": _rel(root, p), "hash": _sha256(p), "cls": "A"})
            except OSError:  # noqa: BLE001 — a file vanishing mid-scan is not fatal
                _log.warning("sync: unreadable capture %s", p)
    return out


def build() -> list[dict]:
    """Full local manifest, sorted by path for stable diffs."""
    root = _root()
    entries = _class_a(root)
    return sorted(entries, key=lambda e: e["path"])


def path_for_hash(digest: str) -> Path | None:
    """Resolve an advertised hash back to a file.

    Only files present in the freshly-built manifest are resolvable, so this
    cannot be walked outside the memory tree regardless of what the caller
    supplies — path traversal is impossible by construction.
    """
    digest = (digest or "").strip().lower()
    if not digest:
        return None
    root = _root()
    for e in build():
        if e["hash"] == digest:
            p = root / e["path"]
            if p.is_file():
                return p
    return None


__all__ = ["build", "path_for_hash"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_manifest.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/ tests/python/memory/sync/
git commit -m "feat(sync): class A manifest over the markdown memory tree"
```

---

## Task 2: Stamp `origin` / `rev` / `updated_by` onto OKF nodes

**Files:**
- Create: `aiforge_core/memory/sync/identity.py`
- Modify: `aiforge_core/memory/okf/store.py` (inside `save_node`, before the render call)
- Test: `tests/python/memory/sync/test_identity.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_identity.py`:

```python
"""Peer identity and the version stamp applied to mutable nodes."""
from __future__ import annotations


def test_self_id_defaults_to_hostname_slug(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AIFORGE_PEER_ID", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "My Laptop.local")
    from aiforge_core.memory.sync import identity

    assert identity.self_id() == "my-laptop-local"


def test_self_id_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    from aiforge_core.memory.sync import identity

    assert identity.self_id() == "nuc"


def test_stamp_sets_origin_on_first_write(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    from aiforge_core.memory.sync import identity

    meta = identity.stamp({"title": "T"})
    assert meta["origin"] == "nuc"
    assert meta["rev"] == 1
    assert meta["updated_by"] == "nuc"


def test_stamp_bumps_rev_and_records_writer(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "ms")
    from aiforge_core.memory.sync import identity

    meta = identity.stamp({"title": "T", "origin": "nuc", "rev": 46, "updated_by": "nuc"})
    assert meta["origin"] == "nuc"      # origin never changes hands
    assert meta["rev"] == 47
    assert meta["updated_by"] == "ms"


def test_save_node_stamps_frontmatter(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    from aiforge_core.memory.okf import nodes, store

    res = store.save_node("learning", None, {"title": "L"}, "body")
    node = nodes.parse_node(open(res["path"], encoding="utf-8").read())
    assert node["meta"]["origin"] == "nuc"
    assert node["meta"]["rev"] == 1
    assert node["meta"]["updated_by"] == "nuc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aiforge_core.memory.sync.identity'`

- [ ] **Step 3: Write minimal implementation**

Create `aiforge_core/memory/sync/identity.py`:

```python
"""This peer's identity, and the version stamp carried by mutable records.

Ordering uses a per-node counter rather than a timestamp. Peers include other
people's machines whose clocks disagree, sometimes badly; a clock-based
last-writer-wins would hand every conflict to the most wrong clock in the mesh.
"""
from __future__ import annotations

import os
import re
import socket


def _slug(value: str) -> str:
    # Dots are stripped, not kept: this slug becomes a filesystem path component,
    # where a dot invites extension parsing and a leading one makes a hidden file.
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-").lower() or "peer"


def self_id() -> str:
    """Stable short slug naming this peer. ``AIFORGE_PEER_ID`` wins."""
    env = (os.environ.get("AIFORGE_PEER_ID") or "").strip()
    if env:
        return _slug(env)
    return _slug(socket.gethostname())


def stamp(meta: dict) -> dict:
    """Return ``meta`` with ``origin``/``rev``/``updated_by`` advanced for a local write.

    ``origin`` is set once, by whichever peer minted the node, and never changes
    hands afterwards — it is half of the node's identity.
    """
    out = dict(meta or {})
    me = self_id()
    out["origin"] = str(out.get("origin") or me)
    out["rev"] = int(out.get("rev") or 0) + 1
    out["updated_by"] = me
    return out


__all__ = ["self_id", "stamp"]
```

Then in `aiforge_core/memory/okf/store.py`, inside `save_node` (line 162), immediately before
the node is rendered, stamp the metadata. Find the point where `meta` is finalised and add:

```python
    from aiforge_core.memory.sync.identity import stamp as _stamp

    meta = _stamp(meta or {})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_identity.py -v`
Expected: PASS, 5 passed

Then confirm nothing else broke:

Run: `.venv/bin/pytest tests/python/memory -v`
Expected: PASS — no regressions in existing OKF tests

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/identity.py aiforge_core/memory/okf/store.py \
        tests/python/memory/sync/test_identity.py
git commit -m "feat(sync): stamp origin/rev/updated_by onto OKF nodes"
```

---

## Task 3: Shared I/O and layout modules, then class B manifest entries

This task has two halves. First extract the disk primitives and the layout rule into `_io.py`
and `paths.py` — Task 1 put `_root`, `_sha256` and `_rel` directly in `manifest.py`, and four
later modules would otherwise each grow their own copy. Then build class B entries on top of
them.

**Files:**
- Create: `aiforge_core/memory/sync/_io.py`
- Create: `aiforge_core/memory/sync/paths.py`
- Modify: `aiforge_core/memory/sync/manifest.py` (use `_io`, add class B)
- Test: `tests/python/memory/sync/test_io.py`
- Test: `tests/python/memory/sync/test_manifest.py` (append)

### Half one — extract the shared modules

- [ ] **Step A1: Write the failing test for `_io`**

Create `tests/python/memory/sync/test_io.py`:

```python
"""Disk primitives. Every other sync module builds on exactly these."""
from __future__ import annotations

import hashlib


def test_sha256_file_hashes_the_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    p = tmp_path / "f.md"
    p.write_bytes(b"hello")

    assert _io.sha256_file(p) == hashlib.sha256(b"hello").hexdigest()


def test_write_atomic_leaves_no_temp_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    target = tmp_path / "deep" / "nested" / "f.md"
    _io.write_atomic(target, b"body")

    assert target.read_bytes() == b"body"
    assert list(target.parent.glob("*.tmp")) == []


def test_write_atomic_replaces_existing_content(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    target = tmp_path / "f.md"
    _io.write_atomic(target, b"old")
    _io.write_atomic(target, b"new")

    assert target.read_bytes() == b"new"


def test_read_json_returns_empty_on_missing_or_corrupt(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    assert _io.read_json(tmp_path / "absent.json") == {}

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _io.read_json(bad) == {}


def test_write_json_round_trips(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    p = tmp_path / "x.json"
    _io.write_json(p, {"a": 1})

    assert _io.read_json(p) == {"a": 1}


def test_safe_target_rejects_escapes(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    assert _io.safe_target("captures/a.md") is not None
    assert _io.safe_target("../../.ssh/authorized_keys") is None
    assert _io.safe_target("/etc/passwd") is None
    assert _io.safe_target("") is None


def test_is_syncable_refuses_a_symlink(monkeypatch, tmp_path):
    """A symlink under captures/ must never be advertised or served."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    secret = tmp_path / "outside.txt"
    secret.write_text("secret", encoding="utf-8")
    real = tmp_path / "real.md"
    real.write_text("fine", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(secret)

    assert _io.is_syncable(real) is True
    assert _io.is_syncable(link) is False
    assert _io.is_syncable(tmp_path / "absent.md") is False
```

- [ ] **Step A2: Run it and confirm it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_io.py -v`
Expected: FAIL — `ImportError: cannot import name '_io'`

- [ ] **Step A3: Write `_io.py`**

Create `aiforge_core/memory/sync/_io.py`:

```python
"""Disk primitives shared by every sync module.

This is the single place that knows how to find the memory tree, hash a file,
and write one without risking a truncated result. Modules that need any of
those import them from here rather than growing their own copy.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

_log = logging.getLogger("aiforge.sync")


def root() -> Path:
    """The markdown memory tree — the source of truth this whole feature syncs."""
    from aiforge_core.memory.md_store import memory_dir

    return memory_dir()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    """Path relative to the tree root, in the posix form the manifest uses."""
    return path.relative_to(root()).as_posix()


def safe_target(relative: str) -> Path | None:
    """Resolve a manifest-supplied path inside the tree, or None if it escapes.

    A peer supplies these strings, so they are untrusted input: anything that
    resolves to the root itself or outside it is refused rather than clamped.
    """
    if not relative:
        return None
    base = root().resolve()
    try:
        target = (base / relative).resolve()
    except (OSError, ValueError):  # noqa: BLE001 — a hostile path must not raise
        return None
    if target == base or base not in target.parents:
        _log.warning("sync: rejected out-of-tree path %s", relative)
        return None
    return target


def is_syncable(path: Path) -> bool:
    """True for a real file we are willing to advertise to a peer.

    Symlinks are refused. ``Path.glob`` follows them, so a symlink planted under
    ``captures/`` would otherwise be listed in the manifest and its *target*
    served over ``/blob`` — turning a read-only sync endpoint into a way to read
    arbitrary files outside the memory tree.
    """
    return path.is_file() and not path.is_symlink()


def write_atomic(target: Path, body: bytes) -> None:
    """Write via temp file + os.replace so a crash cannot leave a partial file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, target)


def read_json(path: Path) -> dict:
    """Load a JSON record, or {} if it is absent or unreadable.

    Soft-fail is correct here: a corrupt marker file must degrade the node, not
    stop it. The caller treats {} as "no record".
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — hand-edited or truncated JSON must not raise
        _log.warning("sync: unreadable json %s, treating as empty", path)
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, record: dict) -> None:
    write_atomic(path, json.dumps(record, indent=2).encode("utf-8"))


__all__ = ["root", "sha256_file", "rel", "safe_target", "is_syncable",
           "write_atomic", "read_json", "write_json"]
```

- [ ] **Step A4: Run it and confirm it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_io.py -v`
Expected: PASS, 6 passed

- [ ] **Step A5: Write the failing test for `paths`**

Create `tests/python/memory/sync/test_paths.py`:

```python
"""The on-disk layout rule, in one place.

OKF ids are per-scope counters, so (nuc, O-01) and (ms, O-01) are different
objects that both render to O-01.md. These functions are the only thing that
decides where an identity lives.
"""
from __future__ import annotations


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))


def _node(tmp_path, scope: str, origin: str, key: str):
    p = tmp_path / "md" / "okf" / scope / "learnings" / f"{key}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\ntype: learning\nid: "{key}"\norigin: "{origin}"\n'
                 f'rev: 1\nupdated_by: "{origin}"\n---\n\nb\n', encoding="utf-8")
    return p


def test_node_paths_matches_on_origin_not_just_filename(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    mine = _node(tmp_path, "global", "book", "O-01")
    _node(tmp_path, "peers/ms", "ms", "O-01")

    assert paths.node_paths("book", "O-01") == [mine]


def test_node_paths_is_empty_for_an_unknown_identity(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    assert paths.node_paths("nuc", "L-99") == []


def test_tomb_and_lease_paths_are_stable(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    assert paths.tomb_path("nuc", "L-07").as_posix().endswith(
        "okf/.tomb/nuc/L-07.json")
    assert paths.lease_path().as_posix().endswith("okf/.lease.json")


def test_target_for_known_identity_is_updated_in_place(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    mine = _node(tmp_path, "global", "nuc", "L-07")
    entry = {"cls": "B", "origin": "nuc", "key": "L-07",
             "path": "okf/peers/nuc/L-07.md"}   # sender's layout differs

    assert paths.target_for(entry) == mine


def test_target_for_a_new_foreign_node_lands_under_peers(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    entry = {"cls": "B", "origin": "ms", "key": "O-01",
             "path": "okf/global/objectives/O-01.md"}

    assert paths.target_for(entry).as_posix().endswith("okf/peers/ms/O-01.md")


def test_target_for_class_a_uses_the_advertised_path(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    entry = {"cls": "A", "path": "captures/a.md"}

    assert paths.target_for(entry).as_posix().endswith("captures/a.md")


def test_target_for_class_a_still_refuses_to_escape(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    assert paths.target_for({"cls": "A", "path": "../../evil"}) is None


def test_a_hostile_origin_or_key_cannot_climb_the_tree(monkeypatch, tmp_path):
    """origin and key come from a peer's frontmatter — treat them as attacker input."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    root = (tmp_path / "md").resolve()

    for origin, key in (("../../..", "L-07"),
                        ("nuc", "../../../../etc/passwd"),
                        ("..", ".."),
                        ("", "")):
        for p in (paths.peer_node_path(origin, key), paths.tomb_path(origin, key)):
            assert root in p.resolve().parents


def test_target_for_a_tombstone_and_the_lease(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    tomb = paths.target_for({"cls": "B", "origin": "nuc", "key": "L-07",
                             "tomb": True, "path": "x"})
    lease = paths.target_for({"cls": "B", "origin": "", "key": "__lease__",
                              "path": "x"})

    assert tomb.as_posix().endswith("okf/.tomb/nuc/L-07.json")
    assert lease.as_posix().endswith("okf/.lease.json")
```

- [ ] **Step A6: Run it and confirm it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_paths.py -v`
Expected: FAIL — `ImportError: cannot import name 'paths'`

- [ ] **Step A7: Write `paths.py`**

Create `aiforge_core/memory/sync/paths.py`:

```python
"""Where an identity lives on disk. The single owner of the layout rule.

OKF ids are per-scope counters (``aiforge_core/memory/okf/store.py:127``), so
``(nuc, O-01)`` and ``(ms, O-01)`` are unrelated objects that both render to
``O-01.md``. A peer's advertised path is therefore a hint, never an instruction:
trusting it would let one peer silently overwrite another's node.

The rule: an identity already held is updated wherever it currently lives;
anything new from another peer lands under ``okf/peers/<origin>/``. Every peer
derives the same answer from the same inputs, so the layout converges along with
the content.
"""
from __future__ import annotations

import re
from pathlib import Path

from aiforge_core.memory.sync import _io

LEASE_KEY = "__lease__"

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _component(value: str) -> str:
    """Sanitise one untrusted path component.

    ``origin`` and ``key`` arrive from a peer's frontmatter, so they are
    attacker-controlled. Dots are stripped along with separators, which is what
    makes ``".."`` collapse to the empty string rather than climbing the tree.
    """
    return _UNSAFE.sub("-", str(value or "")).strip("-") or "_"


def okf_dir() -> Path:
    return _io.root() / "okf"


def tomb_path(origin: str, key: str) -> Path:
    return okf_dir() / ".tomb" / _component(origin) / f"{_component(key)}.json"


def lease_path() -> Path:
    return okf_dir() / ".lease.json"


def peer_node_path(origin: str, key: str) -> Path:
    return okf_dir() / "peers" / _component(origin) / f"{_component(key)}.md"


def node_paths(origin: str, key: str) -> list[Path]:
    """Every node file on disk carrying this identity, across all scopes."""
    from aiforge_core.memory.okf import nodes as _nodes

    okf = okf_dir()
    if not okf.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(okf.rglob(f"{key}.md")):
        if p.name.endswith(".conflict.md"):
            continue
        try:
            meta = (_nodes.parse_node(p.read_text(encoding="utf-8")).get("meta") or {})
        except Exception:  # noqa: BLE001 — an unreadable node is left untouched
            continue
        if str(meta.get("origin") or "") == origin:
            out.append(p)
    return out


def target_for(entry: dict) -> Path | None:
    """Local destination for a manifest entry, or None if it must be refused."""
    if entry.get("cls") == "A":
        # Capture filenames embed a content digest, so they are globally unique.
        return _io.safe_target(str(entry.get("path") or ""))

    key = str(entry.get("key") or "")
    if not key:
        return None
    origin = str(entry.get("origin") or "")

    if entry.get("tomb"):
        return tomb_path(origin, key)
    if key == LEASE_KEY:
        return lease_path()

    existing = node_paths(origin, key)
    return existing[0] if existing else peer_node_path(origin, key)


__all__ = ["okf_dir", "tomb_path", "lease_path", "peer_node_path", "node_paths",
           "target_for", "LEASE_KEY"]
```

- [ ] **Step A8: Run it and confirm it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_paths.py -v`
Expected: PASS, 8 passed

- [ ] **Step A9: Refactor `manifest.py` onto `_io`, confirm no behaviour change**

Delete `_root`, `_sha256` and `_rel` from `manifest.py` and use `_io.root()`,
`_io.sha256_file()` and `_io.rel()` instead. Replace the body of `path_for_hash` so it uses
`_io.root()`. Do not change any behaviour.

One behaviour *is* added here, and it is a security fix rather than a refactor: guard the
`_class_a` scan with `_io.is_syncable(p)` so symlinks are neither advertised nor servable.
Add a test for it alongside the existing manifest tests:

```python
def test_a_symlinked_capture_is_never_advertised(monkeypatch, tmp_path):
    """Path.glob follows symlinks; /blob must not become an arbitrary file reader."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    d = tmp_path / "md" / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / "real-20260719-aaaaaa.md").write_text("fine", encoding="utf-8")
    (d / "evil-20260719-bbbbbb.md").symlink_to(secret)

    paths = {e["path"] for e in manifest.build()}
    assert paths == {"captures/real-20260719-aaaaaa.md"}
    assert manifest.path_for_hash(
        hashlib.sha256(b"classified").hexdigest()) is None
```

Run: `.venv/bin/pytest tests/python/memory/sync/ -v`
Expected: PASS — the Task 1 tests still pass unchanged (the point of the refactor), plus the
new symlink test

- [ ] **Step A10: Commit half one**

```bash
git add aiforge_core/memory/sync/_io.py aiforge_core/memory/sync/paths.py \
        aiforge_core/memory/sync/manifest.py \
        tests/python/memory/sync/test_io.py tests/python/memory/sync/test_paths.py
git commit -m "refactor(sync): extract shared disk primitives and the layout rule"
```

### Half two — class B manifest entries

- [ ] **Step 1: Write the failing test**

Append to `tests/python/memory/sync/test_manifest.py`:

```python
def test_class_b_entry_from_node_frontmatter(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    d = tmp_path / "md" / "okf" / "global" / "learnings"
    d.mkdir(parents=True, exist_ok=True)
    (d / "L-07.md").write_text(
        '---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 47\n'
        'updated_by: "ms"\n---\n\nbody\n',
        encoding="utf-8",
    )

    b = [e for e in manifest.build() if e["cls"] == "B"]
    assert len(b) == 1
    assert b[0]["origin"] == "nuc"
    assert b[0]["key"] == "L-07"
    assert b[0]["rev"] == 47
    assert b[0]["updated_by"] == "ms"


def test_unstamped_node_is_local_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    d = tmp_path / "md" / "okf" / "global" / "learnings"
    d.mkdir(parents=True, exist_ok=True)
    (d / "L-09.md").write_text('---\ntype: learning\nid: "L-09"\n---\n\nbody\n',
                               encoding="utf-8")

    assert [e for e in manifest.build() if e["cls"] == "B"] == []


def test_index_and_conflict_sidecars_never_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    okf = tmp_path / "md" / "okf"
    (okf / "global" / "learnings").mkdir(parents=True, exist_ok=True)
    (okf / "index.md").write_text("# index\n", encoding="utf-8")
    (okf / "global" / "learnings" / "L-07.conflict.md").write_text("loser\n",
                                                                   encoding="utf-8")

    assert manifest.build() == []


def test_tombstone_and_lease_are_class_b(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    okf = tmp_path / "md" / "okf"
    (okf / ".tomb" / "nuc").mkdir(parents=True, exist_ok=True)
    (okf / ".tomb" / "nuc" / "L-07.json").write_text(
        '{"origin":"nuc","key":"L-07","rev":48,"updated_by":"nuc","tomb":true}',
        encoding="utf-8",
    )
    (okf / ".lease.json").write_text(
        '{"origin":"","key":"__lease__","rev":3,"updated_by":"nuc",'
        '"holder":"nuc","expires_at":1763000000}',
        encoding="utf-8",
    )

    by_key = {e["key"]: e for e in manifest.build()}
    assert by_key["L-07"]["tomb"] is True
    assert by_key["L-07"]["rev"] == 48
    assert by_key["__lease__"]["rev"] == 3
    assert by_key["__lease__"]["origin"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_manifest.py -v`
Expected: FAIL — the four new tests fail; `build()` returns only class A entries

- [ ] **Step 3: Write minimal implementation**

In `aiforge_core/memory/sync/manifest.py`, add the class B builder and wire it into `build()`.
Note this half uses `_io` and `paths` throughout — it must not reintroduce a local `_root`,
`_sha256` or `_rel`, and it must not hand-roll the tombstone or lease path, which `paths` owns.

```python
def _entry_for_node(p: Path) -> dict | None:
    from aiforge_core.memory.okf import nodes as _nodes

    try:
        node = _nodes.parse_node(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a hand-edited node must not break the manifest
        _log.warning("sync: unreadable node %s", p)
        return None
    meta = node.get("meta") or {}
    origin = str(meta.get("origin") or "")
    key = str(meta.get("id") or "")
    if not origin or not key:
        # Not yet stamped by identity.stamp(); stays local until it is written again.
        return None
    return {
        "path": _io.rel(p),
        "hash": _io.sha256_file(p),
        "cls": "B",
        "origin": origin,
        "key": key,
        "rev": int(meta.get("rev") or 0),
        "updated_by": str(meta.get("updated_by") or origin),
    }


def _entry_for_json(p: Path) -> dict | None:
    rec = _io.read_json(p)
    if not rec.get("key"):
        return None
    entry = {
        "path": _io.rel(p),
        "hash": _io.sha256_file(p),
        "cls": "B",
        "origin": str(rec.get("origin") or ""),
        "key": str(rec.get("key")),
        "rev": int(rec.get("rev") or 0),
        "updated_by": str(rec.get("updated_by") or ""),
    }
    if rec.get("tomb"):
        entry["tomb"] = True
    return entry


def _class_b() -> list[dict]:
    okf = paths.okf_dir()
    if not okf.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(okf.rglob("*.md")):
        if p.name == "index.md" or p.name.endswith(".conflict.md"):
            # index.md is regenerated locally; sidecars are local-only by design.
            continue
        entry = _entry_for_node(p)
        if entry:
            out.append(entry)
    tomb = okf / ".tomb"
    if tomb.is_dir():
        for p in sorted(tomb.rglob("*.json")):
            entry = _entry_for_json(p)
            if entry:
                out.append(entry)
    lease = paths.lease_path()
    if lease.is_file():
        entry = _entry_for_json(lease)
        if entry:
            out.append(entry)
    return out
```

Change `build()` to include them:

```python
def build() -> list[dict]:
    """Full local manifest, sorted by path for stable diffs."""
    return sorted(_class_a() + _class_b(), key=lambda e: e["path"])
```

Imports at the top of the module become:

```python
from aiforge_core.memory.sync import _io, paths
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_manifest.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/manifest.py tests/python/memory/sync/test_manifest.py
git commit -m "feat(sync): class B manifest entries, tombstones and lease"
```

---

# Phase 2 — The pure merge

## Task 4: Merge decisions for both classes

> **Partly superseded by remediation R1.** An adversarial review later proved three
> defects live in the code below. `int(rev)` raises on a non-numeric revision and
> aborts the *entire* merge rather than skipping one entry, so a single hand-edited
> node on any peer takes the mesh's sync offline. The `(rev, updated_by)` ordering is
> not total: two peers at equal revision with the same writer and differing content
> deadlock permanently, because the comparison is false in both directions. And a
> remote entry missing `hash` matches the `None` that an unhashed local entry puts in
> the `have` set, so a real capture is dropped as already-present with no log line.
> The committed `merge.py` adds `as_rev()`, orders on `(rev, updated_by, hash)`, and
> skips unhashed entries loudly. Read the committed module, not this block.

`merge.py` is deliberately pure: two lists in, a decision set out, no filesystem and no
network. This is where the interesting logic lives, so it must be testable without a second
machine.

**Files:**
- Create: `aiforge_core/memory/sync/merge.py`
- Test: `tests/python/memory/sync/test_merge.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_merge.py`:

```python
"""Merge rules. Pure functions — no filesystem, no network."""
from __future__ import annotations

from aiforge_core.memory.sync import merge


def a(path: str, h: str) -> dict:
    return {"path": path, "hash": h, "cls": "A"}


def b(key: str, rev: int, by: str, h: str, *, origin: str = "nuc",
      tomb: bool = False) -> dict:
    e = {"path": f"okf/global/learnings/{key}.md", "hash": h, "cls": "B",
         "origin": origin, "key": key, "rev": rev, "updated_by": by}
    if tomb:
        e["tomb"] = True
        e["path"] = f"okf/.tomb/{origin}/{key}.json"
    return e


def test_class_a_wants_only_missing_hashes():
    local = [a("captures/x.md", "h1")]
    remote = [a("captures/x.md", "h1"), a("captures/y.md", "h2")]

    plan = merge.plan_sync(local, remote)

    assert [e["hash"] for e in plan["want"]] == ["h2"]
    assert plan["conflict"] == []


def test_class_a_same_content_different_name_is_not_wanted():
    # Content-addressed: identity is the hash, not the path.
    local = [a("captures/x.md", "h1")]
    remote = [a("captures/renamed.md", "h1")]

    assert merge.plan_sync(local, remote)["want"] == []


def test_class_b_unknown_identity_is_wanted():
    plan = merge.plan_sync([], [b("L-07", 1, "nuc", "h1")])
    assert [e["key"] for e in plan["want"]] == ["L-07"]


def test_class_b_higher_rev_wins():
    local = [b("L-07", 46, "nuc", "h1")]
    remote = [b("L-07", 47, "ms", "h2")]

    plan = merge.plan_sync(local, remote)

    assert [e["rev"] for e in plan["want"]] == [47]
    assert plan["conflict"] == []


def test_class_b_lower_rev_is_ignored():
    local = [b("L-07", 47, "ms", "h2")]
    remote = [b("L-07", 46, "nuc", "h1")]

    plan = merge.plan_sync(local, remote)

    assert plan["want"] == []
    assert plan["conflict"] == []


def test_class_b_identical_hash_is_a_no_op():
    local = [b("L-07", 47, "ms", "h2")]
    remote = [b("L-07", 47, "ms", "h2")]

    assert merge.plan_sync(local, remote) == {"want": [], "conflict": []}


def test_same_rev_different_content_is_a_conflict_with_a_deterministic_winner():
    local = [b("L-07", 47, "alice", "h1")]
    remote = [b("L-07", 47, "bob", "h2")]

    plan = merge.plan_sync(local, remote)

    # 'bob' > 'alice' lexicographically, so the remote wins and is fetched...
    assert [e["updated_by"] for e in plan["want"]] == ["bob"]
    # ...but the collision is still reported so the loser can be kept.
    assert len(plan["conflict"]) == 1
    assert plan["conflict"][0]["local"]["updated_by"] == "alice"


def test_same_rev_conflict_where_local_wins_reports_but_does_not_fetch():
    local = [b("L-07", 47, "bob", "h2")]
    remote = [b("L-07", 47, "alice", "h1")]

    plan = merge.plan_sync(local, remote)

    assert plan["want"] == []
    assert len(plan["conflict"]) == 1


def test_same_origin_and_key_across_different_scopes_is_one_identity():
    local = [b("L-07", 46, "nuc", "h1")]
    remote = [dict(b("L-07", 47, "nuc", "h2"),
                   path="okf/projects/oneshell/learnings/L-07.md")]

    plan = merge.plan_sync(local, remote)

    assert [e["rev"] for e in plan["want"]] == [47]


def test_different_origins_with_the_same_key_are_different_objects():
    local = [b("O-01", 5, "nuc", "h1", origin="nuc")]
    remote = [b("O-01", 1, "ms", "h2", origin="ms")]

    plan = merge.plan_sync(local, remote)

    assert [e["origin"] for e in plan["want"]] == ["ms"]


def test_tombstone_beats_an_older_edit():
    local = [b("L-07", 47, "nuc", "h1")]
    remote = [b("L-07", 48, "nuc", "h2", tomb=True)]

    plan = merge.plan_sync(local, remote)

    assert plan["want"][0].get("tomb") is True


def test_a_newer_edit_beats_a_tombstone():
    local = [b("L-07", 48, "nuc", "h2", tomb=True)]
    remote = [b("L-07", 49, "ms", "h3")]

    plan = merge.plan_sync(local, remote)

    assert plan["want"][0].get("tomb") is None


def test_lease_is_a_singleton_ordered_on_rev():
    local = [{"path": "okf/.lease.json", "hash": "h1", "cls": "B", "origin": "",
              "key": "__lease__", "rev": 3, "updated_by": "nuc"}]
    remote = [{"path": "okf/.lease.json", "hash": "h2", "cls": "B", "origin": "",
               "key": "__lease__", "rev": 4, "updated_by": "ms"}]

    plan = merge.plan_sync(local, remote)

    assert [e["rev"] for e in plan["want"]] == [4]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_merge.py -v`
Expected: FAIL — `ImportError: cannot import name 'merge'`

- [ ] **Step 3: Write minimal implementation**

Create `aiforge_core/memory/sync/merge.py`:

```python
"""Merge rules for the sync protocol. Pure — no filesystem, no network.

Class A records are immutable and content-addressed, so union by hash converges
without coordination. Class B records are identified by ``(origin, key)`` and
ordered by ``(rev, updated_by)``: a higher revision wins, and an equal revision
breaks on the writer's slug so every peer independently reaches the same answer.

An equal revision with differing content means two peers edited the same node
before either synced. The winner is still deterministic, but the collision is
reported so the caller can preserve the losing text rather than discard it.
"""
from __future__ import annotations


def _ident(entry: dict) -> tuple[str, str]:
    return (str(entry.get("origin") or ""), str(entry.get("key") or ""))


def _order(entry: dict) -> tuple[int, str]:
    return (int(entry.get("rev") or 0), str(entry.get("updated_by") or ""))


def plan_sync(local: list[dict], remote: list[dict]) -> dict:
    """Decide what to fetch from a peer.

    Returns ``{"want": [remote entries to fetch], "conflict": [{local, remote}]}``.
    A conflicting entry may also appear in ``want`` when the remote is the winner.
    """
    have = {e.get("hash") for e in local if e.get("cls") == "A"}
    by_ident = {_ident(e): e for e in local if e.get("cls") == "B"}

    want: list[dict] = []
    conflict: list[dict] = []

    for r in remote:
        if r.get("cls") == "A":
            if r.get("hash") not in have:
                want.append(r)
            continue

        cur = by_ident.get(_ident(r))
        if cur is None:
            want.append(r)
            continue
        if cur.get("hash") == r.get("hash"):
            continue

        if int(cur.get("rev") or 0) == int(r.get("rev") or 0):
            conflict.append({"local": cur, "remote": r})
        if _order(r) > _order(cur):
            want.append(r)

    return {"want": want, "conflict": conflict}


__all__ = ["plan_sync"]
```

Update `aiforge_core/memory/sync/__init__.py` `__all__` to include `identity`:

```python
__all__ = ["identity", "manifest", "merge"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_merge.py -v`
Expected: PASS, 13 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/merge.py aiforge_core/memory/sync/__init__.py \
        tests/python/memory/sync/test_merge.py
git commit -m "feat(sync): pure merge rules for class A union and class B LWW"
```

---

# Phase 3 — Transport

## Task 5: `peers.json` store

**Files:**
- Create: `aiforge_core/memory/sync/peers.py`
- Test: `tests/python/memory/sync/test_peers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_peers.py`:

```python
"""peers.json: identity, approval state, and the gossiped roster."""
from __future__ import annotations

import json


def test_load_returns_empty_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    from aiforge_core.memory.sync import peers

    assert peers.load() == {"self": {}, "peers": []}


def test_approved_filters_out_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": [
        {"id": "nuc", "urls": ["http://a"], "token": "t", "state": "approved"},
        {"id": "eve", "urls": ["http://b"], "state": "candidate"},
    ]})

    assert [p["id"] for p in peers.approved()] == ["nuc"]


def test_roster_never_exposes_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book", "urls": ["http://me"]}, "peers": [
        {"id": "nuc", "urls": ["http://a"], "token": "SECRET", "state": "approved"},
    ]})

    blob = json.dumps(peers.roster())
    assert "SECRET" not in blob
    assert {"book", "nuc"} == {r["id"] for r in peers.roster()}


def test_gossip_learns_new_peers_as_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": [
        {"id": "nuc", "urls": ["http://a"], "token": "t", "state": "approved"},
    ]})
    peers.merge_roster([{"id": "alice", "urls": ["http://c"], "last_seen": 5}])

    got = {p["id"]: p for p in peers.load()["peers"]}
    assert got["alice"]["state"] == "candidate"
    assert got["alice"]["urls"] == ["http://c"]


def test_gossip_never_promotes_or_grants_a_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": []})
    # A hostile peer claims to be approved and supplies its own token.
    peers.merge_roster([{"id": "eve", "urls": ["http://evil"],
                         "state": "approved", "token": "PWNED"}])

    got = peers.load()["peers"][0]
    assert got["state"] == "candidate"
    assert "token" not in got
    assert peers.approved() == []


def test_gossip_does_not_downgrade_an_approved_peer(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": [
        {"id": "nuc", "urls": ["http://a"], "token": "t", "state": "approved"},
    ]})
    peers.merge_roster([{"id": "nuc", "urls": ["http://a", "http://b"]}])

    got = peers.load()["peers"][0]
    assert got["state"] == "approved"
    assert got["token"] == "t"
    assert got["urls"] == ["http://a", "http://b"]   # addresses do update


def test_gossip_ignores_self(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": []})
    peers.merge_roster([{"id": "book", "urls": ["http://me"]}])

    assert peers.load()["peers"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_peers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aiforge_core.memory.sync.peers'`

- [ ] **Step 3: Write minimal implementation**

Create `aiforge_core/memory/sync/peers.py`:

```python
"""Peer registry — ``$AIFORGE_CONFIG_DIR/peers.json``.

This is local configuration, not memory: it is never synced and never appears
in the manifest. The gossiped roster is merged *into* it, but discovery is not
trust — a learned peer lands in ``candidate`` state, is never pulled from, and
is promoted only when a human supplies a token obtained out of band.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from aiforge_core.memory.sync import _io

_log = logging.getLogger("aiforge.sync")

STATE_APPROVED = "approved"
STATE_CANDIDATE = "candidate"


def _path() -> Path:
    # peers.json is CONFIG, not memory — it lives beside the other config files
    # and is never synced, so it does not go under the memory tree.
    d = Path(os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")))
    d.mkdir(parents=True, exist_ok=True)
    return d / "peers.json"


def load() -> dict:
    data = _io.read_json(_path())
    return {"self": data.get("self") or {}, "peers": data.get("peers") or []}


def save(data: dict) -> dict:
    _io.write_json(_path(), data)
    return data


def approved() -> list[dict]:
    """Peers this node is willing to pull from."""
    return [p for p in load()["peers"] if p.get("state") == STATE_APPROVED]


def roster() -> list[dict]:
    """What this node advertises to others. Ids and urls only — never tokens."""
    from aiforge_core.memory.sync.identity import self_id

    data = load()
    me = data["self"]
    out = [{"id": self_id(), "urls": list(me.get("urls") or []),
            "last_seen": int(time.time())}]
    for p in data["peers"]:
        out.append({"id": p.get("id"), "urls": list(p.get("urls") or []),
                    "last_seen": int(p.get("last_seen") or 0)})
    return out


def merge_roster(entries: list[dict]) -> dict:
    """Fold a peer's advertised roster into the local registry.

    Unknown peers are recorded as candidates. Nothing in a roster can promote a
    peer or grant a token: state and token fields arriving over the wire are
    dropped, so a compromised peer can add noise but never mesh membership.
    """
    from aiforge_core.memory.sync.identity import self_id

    me = self_id()
    data = load()
    index = {p.get("id"): p for p in data["peers"]}

    for raw in entries or []:
        pid = str((raw or {}).get("id") or "").strip()
        if not pid or pid == me:
            continue
        urls = [str(u) for u in (raw.get("urls") or []) if u]
        seen = int(raw.get("last_seen") or 0)
        cur = index.get(pid)
        if cur is None:
            index[pid] = {"id": pid, "urls": urls, "state": STATE_CANDIDATE,
                          "last_seen": seen}
            _log.info("sync: discovered candidate peer %s", pid)
            continue
        if urls:
            cur["urls"] = urls
        cur["last_seen"] = max(int(cur.get("last_seen") or 0), seen)

    data["peers"] = list(index.values())
    return save(data)


def touch(peer_id: str) -> None:
    """Record a successful contact so a peer ages out of the roster only when dead."""
    data = load()
    for p in data["peers"]:
        if p.get("id") == peer_id:
            p["last_seen"] = int(time.time())
    save(data)


__all__ = ["load", "save", "approved", "roster", "merge_roster", "touch",
           "STATE_APPROVED", "STATE_CANDIDATE"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_peers.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/peers.py tests/python/memory/sync/test_peers.py
git commit -m "feat(sync): peers.json registry with candidate quarantine"
```

---

## Task 6: The two HTTP endpoints

**Files:**
- Create: `aiforge_core/api/routes/sync.py`
- Modify: `aiforge_core/api/api.py:63-92` (import + `include_router`)
- Test: `tests/python/memory/sync/test_sync_routes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_sync_routes.py`:

```python
"""The two read-only sync endpoints."""
from __future__ import annotations

import hashlib
import importlib

from fastapi.testclient import TestClient


def _fresh_api(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_API_TOKEN", "AIFORGE_BIND_HOST"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return api


def _seed_capture(tmp_path, text: str) -> str:
    d = tmp_path / "md" / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / "a-20260719-aaaaaa.md").write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def test_manifest_returns_entries_and_roster(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    digest = _seed_capture(tmp_path, "hello")

    r = TestClient(api.app).get("/api/memory/sync/manifest")

    assert r.status_code == 200
    body = r.json()
    assert [e["hash"] for e in body["manifest"]] == [digest]
    assert body["roster"][0]["id"] == "book"


def test_blob_returns_bytes_for_an_advertised_hash(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    digest = _seed_capture(tmp_path, "hello")

    r = TestClient(api.app).get(f"/api/memory/sync/blob/{digest}")

    assert r.status_code == 200
    assert r.content == b"hello"


def test_blob_404s_for_an_unknown_hash(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    _seed_capture(tmp_path, "hello")

    r = TestClient(api.app).get("/api/memory/sync/blob/" + "0" * 64)

    assert r.status_code == 404


def test_endpoints_require_the_api_token_when_one_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_API_TOKEN", "s3cret")
    api = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_API_TOKEN", "s3cret")
    client = TestClient(api.app)

    assert client.get("/api/memory/sync/manifest").status_code == 401
    ok = client.get("/api/memory/sync/manifest",
                    headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_sync_routes.py -v`
Expected: FAIL — all four return 404, the routes do not exist

- [ ] **Step 3: Write minimal implementation**

Create `aiforge_core/api/routes/sync.py`:

```python
"""Peer sync routes (/api/memory/sync/*) — read-only, pull-only.

These endpoints only ever read. A peer cannot delete, overwrite, or push
anything through them; the puller decides what it wants. Bearer auth is
inherited from the ``/api/`` middleware in ``aiforge_core/api/api.py``, so no
per-route dependency is needed.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response

router = APIRouter()

_af_log = logging.getLogger("aiforge")


@router.get("/api/memory/sync/manifest")
def sync_manifest() -> dict:
    from aiforge_core.memory.sync import manifest as _man
    from aiforge_core.memory.sync import peers as _peers

    return {"manifest": _man.build(), "roster": _peers.roster()}


@router.get("/api/memory/sync/blob/{digest}")
def sync_blob(digest: str) -> Response:
    from aiforge_core.memory.sync import manifest as _man

    path = _man.path_for_hash(digest)
    if path is None:
        raise HTTPException(404, f"no blob: {digest}")
    return Response(content=path.read_bytes(), media_type="text/markdown")
```

In `aiforge_core/api/api.py`, alongside the other route imports (around line 63-77) add:

```python
from aiforge_core.api.routes import sync as _r_sync  # noqa: E402
```

and alongside the other `include_router` calls (around line 78-92) add:

```python
app.include_router(_r_sync.router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_sync_routes.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/api/routes/sync.py aiforge_core/api/api.py \
        tests/python/memory/sync/test_sync_routes.py
git commit -m "feat(sync): read-only manifest and blob endpoints"
```

---

## Task 7: Transport and apply, as two separate concerns

"Talk HTTP to a peer" and "decide where a file goes on disk" fail differently, are tested
differently, and change for different reasons — so they are two modules. `apply.py` never
imports httpx; `transport.py` never touches a path. Both build on `_io` and `paths`; neither
reimplements hashing, atomic writes, or the layout rule.

**Files:**
- Create: `aiforge_core/memory/sync/transport.py`
- Create: `aiforge_core/memory/sync/apply.py`
- Test: `tests/python/memory/sync/test_apply.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_apply.py`. Note the target-path rules are already covered
by `test_paths.py` — do not duplicate them here. These tests cover verification, the
node/tombstone invariant, and sidecars.

```python
"""Applying a fetched blob to the local tree: verify, place, preserve."""
from __future__ import annotations

import hashlib


def _md(tmp_path):
    d = tmp_path / "md"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_apply_writes_a_class_a_blob(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    body = b"hello"
    entry = {"path": "captures/a.md", "hash": hashlib.sha256(body).hexdigest(),
             "cls": "A"}

    assert apply.apply_blob(entry, body) is True
    assert (tmp_path / "md" / "captures" / "a.md").read_bytes() == body


def test_apply_rejects_a_hash_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    entry = {"path": "captures/a.md", "hash": "0" * 64, "cls": "A"}

    assert apply.apply_blob(entry, b"tampered") is False
    assert not (tmp_path / "md" / "captures" / "a.md").exists()


def test_apply_refuses_to_escape_the_memory_tree(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    body = b"pwn"
    entry = {"path": "../../.ssh/authorized_keys",
             "hash": hashlib.sha256(body).hexdigest(), "cls": "A"}

    assert apply.apply_blob(entry, body) is False
    assert not (tmp_path / ".ssh").exists()


def test_applying_a_tombstone_removes_the_node(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    node = tmp_path / "md" / "okf" / "global" / "learnings" / "L-07.md"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text('---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 47\n'
                    'updated_by: "nuc"\n---\n\nbody\n', encoding="utf-8")

    body = b'{"origin":"nuc","key":"L-07","rev":48,"updated_by":"nuc","tomb":true}'
    entry = {"path": "okf/.tomb/nuc/L-07.json",
             "hash": hashlib.sha256(body).hexdigest(), "cls": "B",
             "origin": "nuc", "key": "L-07", "rev": 48, "updated_by": "nuc",
             "tomb": True}

    assert apply.apply_blob(entry, body) is True
    assert not node.exists()
    assert (tmp_path / "md" / "okf" / ".tomb" / "nuc" / "L-07.json").exists()


def test_applying_a_node_removes_its_tombstone(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    tomb = tmp_path / "md" / "okf" / ".tomb" / "nuc" / "L-07.json"
    tomb.parent.mkdir(parents=True, exist_ok=True)
    tomb.write_text('{"origin":"nuc","key":"L-07","rev":48,'
                    '"updated_by":"nuc","tomb":true}', encoding="utf-8")

    body = b'---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 49\nupdated_by: "ms"\n---\n\nnew\n'
    entry = {"path": "okf/global/learnings/L-07.md",
             "hash": hashlib.sha256(body).hexdigest(), "cls": "B",
             "origin": "nuc", "key": "L-07", "rev": 49, "updated_by": "ms"}

    assert apply.apply_blob(entry, body) is True
    assert not tomb.exists()


def test_foreign_node_lands_under_peers_not_over_a_local_id(monkeypatch, tmp_path):
    """(nuc, O-01) and (ms, O-01) are different objects with the same filename."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import apply

    mine = tmp_path / "md" / "okf" / "global" / "objectives" / "O-01.md"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text('---\ntype: objective\nid: "O-01"\norigin: "book"\nrev: 3\n'
                    'updated_by: "book"\n---\n\nmine\n', encoding="utf-8")

    body = (b'---\ntype: objective\nid: "O-01"\norigin: "ms"\nrev: 1\n'
            b'updated_by: "ms"\n---\n\ntheirs\n')
    entry = {"path": "okf/global/objectives/O-01.md",
             "hash": hashlib.sha256(body).hexdigest(), "cls": "B",
             "origin": "ms", "key": "O-01", "rev": 1, "updated_by": "ms"}

    assert apply.apply_blob(entry, body) is True
    assert "mine" in mine.read_text(encoding="utf-8")          # untouched
    assert (tmp_path / "md" / "okf" / "peers" / "ms" / "O-01.md").exists()


def test_an_update_to_an_existing_identity_lands_in_place(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import apply

    node = tmp_path / "md" / "okf" / "global" / "learnings" / "L-07.md"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text('---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 47\n'
                    'updated_by: "nuc"\n---\n\nold\n', encoding="utf-8")

    body = (b'---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 48\n'
            b'updated_by: "ms"\n---\n\nnew\n')
    entry = {"path": "okf/peers/nuc/L-07.md",   # the peer's own layout differs
             "hash": hashlib.sha256(body).hexdigest(), "cls": "B",
             "origin": "nuc", "key": "L-07", "rev": 48, "updated_by": "ms"}

    assert apply.apply_blob(entry, body) is True
    assert "new" in node.read_text(encoding="utf-8")           # updated in place
    assert not (tmp_path / "md" / "okf" / "peers" / "nuc" / "L-07.md").exists()


def test_conflict_writes_a_sidecar_beside_the_loser(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    node = tmp_path / "md" / "okf" / "global" / "learnings" / "L-07.md"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text("local version\n", encoding="utf-8")

    apply.keep_conflict({"path": "okf/global/learnings/L-07.md",
                          "key": "L-07", "updated_by": "alice"})

    sidecar = node.parent / "L-07.conflict.md"
    assert sidecar.read_text(encoding="utf-8") == "local version\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_apply.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply' from 'aiforge_core.memory.sync'`

- [ ] **Step 3: Write minimal implementation**

Create `aiforge_core/memory/sync/transport.py` — HTTP only, never touches a path:

```python
"""Fetching from one peer over HTTP. Knows nothing about disk.

An unreachable peer is normal operation, not an error: pull-only means nothing
is queued for it and nothing blocks on it, so every failure here degrades to
"nothing new this cycle" and is retried on the next one.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("aiforge.sync")

TIMEOUT = 20.0


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch_manifest(base_url: str, token: str = "") -> dict:
    """GET a peer's manifest. Returns {} when the peer is unreachable."""
    import httpx

    try:
        r = httpx.get(f"{base_url.rstrip('/')}/api/memory/sync/manifest",
                      headers=_headers(token), timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001 — an unreachable peer is expected, not exceptional
        _log.info("sync: peer %s unreachable: %s", base_url, exc)
        return {}
    return data if isinstance(data, dict) else {}


def fetch_blob(base_url: str, digest: str, token: str = "") -> bytes | None:
    """GET one blob by hash. Returns None on any failure; retried next cycle."""
    import httpx

    try:
        r = httpx.get(f"{base_url.rstrip('/')}/api/memory/sync/blob/{digest}",
                      headers=_headers(token), timeout=TIMEOUT)
        r.raise_for_status()
        return r.content
    except Exception as exc:  # noqa: BLE001 — retried on the next cycle
        _log.info("sync: blob %s from %s failed: %s", digest[:8], base_url, exc)
        return None


__all__ = ["fetch_manifest", "fetch_blob", "TIMEOUT"]
```

Create `aiforge_core/memory/sync/apply.py` — disk only, never imports httpx:

```python
"""Placing a fetched blob into the local tree. Knows nothing about HTTP.

Every blob is verified against the hash its peer advertised before it touches
the tree, and every write goes through ``_io.write_atomic``, so an interrupted
or tampered fetch can never leave a partial or forged note behind. A rejected
blob is simply dropped — it reappears in the next diff.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from aiforge_core.memory.sync import _io, paths

_log = logging.getLogger("aiforge.sync")


def apply_blob(entry: dict, body: bytes) -> bool:
    """Verify and write one fetched blob. False means it was rejected."""
    if hashlib.sha256(body).hexdigest() != str(entry.get("hash") or ""):
        _log.warning("sync: hash mismatch for %s, dropping", entry.get("path"))
        return False

    target = paths.target_for(entry)
    if target is None:
        return False

    _io.write_atomic(target, body)
    _enforce_invariant(entry)
    return True


def _enforce_invariant(entry: dict) -> None:
    """For one (origin, key), either the node file or its tombstone exists, never both."""
    if entry.get("cls") != "B":
        return
    key = str(entry.get("key") or "")
    if not key:
        return
    origin = str(entry.get("origin") or "")
    if entry.get("tomb"):
        for p in paths.node_paths(origin, key):
            p.unlink(missing_ok=True)
    else:
        paths.tomb_path(origin, key).unlink(missing_ok=True)


def keep_conflict(local_entry: dict) -> Path | None:
    """Preserve a losing local version beside the node as a ``.conflict`` sidecar.

    Sidecars are local artefacts, excluded from the manifest: replicating them
    would multiply one collision across the whole mesh.
    """
    target = _io.safe_target(str(local_entry.get("path") or ""))
    if target is None or not target.is_file():
        return None
    sidecar = target.with_name(target.stem + ".conflict.md")
    try:
        _io.write_atomic(sidecar, target.read_bytes())
    except OSError:  # noqa: BLE001 — losing the sidecar must not abort the sync
        _log.warning("sync: could not write conflict sidecar for %s", target)
        return None
    _log.info("sync: conflict on %s, kept losing version at %s",
              local_entry.get("key"), sidecar.name)
    return sidecar


__all__ = ["apply_blob", "keep_conflict"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_apply.py -v`
Expected: PASS, 8 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/transport.py aiforge_core/memory/sync/apply.py \
        tests/python/memory/sync/test_apply.py
git commit -m "feat(sync): split transport (HTTP) from apply (disk)"
```

---

## Task 8: The sync loop, plus a two-peer in-process test

> **The test harness below is broken as written — see the committed
> `test_two_peer.py` instead.** `memory_dir()` re-reads `AIFORGE_MEMORY_MD_DIR` on
> every call and the env is process-wide, so `_pull` activating the destination and
> then serving through the source's `TestClient` makes the "source" read the
> *destination's* tree. Three of the five tests fail against a correct `loop.py`, and
> `test_a_tampered_blob_is_rejected` passes vacuously — empty manifest, nothing
> fetched, `rejected == 0`. The committed version adds a `_serving(peer)` context
> manager that swaps the env around each faked call, and asserts the two trees are
> distinct before the sync and identical after, with each arrived file physically
> present under the receiving peer's own directory.

**Files:**
- Create: `aiforge_core/memory/sync/loop.py`
- Test: `tests/python/memory/sync/test_two_peer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_two_peer.py`:

```python
"""Two peers converge. The headline behaviour of the whole feature."""
from __future__ import annotations

import hashlib
import importlib

from fastapi.testclient import TestClient


def _peer(monkeypatch, tmp_path, name: str):
    """Build an isolated peer: its own config dir, memory dir, and API app."""
    home = tmp_path / name
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(home / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(home / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(home / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_PEER_ID", name)
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_API_TOKEN", "AIFORGE_PG_URL"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return {"name": name, "home": home, "client": TestClient(api.app)}


def _activate(monkeypatch, peer) -> None:
    """Point the process-wide env at this peer (only one is 'current' at a time)."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(peer["home"] / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(peer["home"] / "md"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer["name"])


def _write_capture(peer, name: str, text: str) -> None:
    d = peer["home"] / "md" / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _pull(monkeypatch, dst, src) -> dict:
    """Run one cycle: dst pulls from src, using src's TestClient as transport."""
    from aiforge_core.memory.sync import loop

    def _fetch_manifest(base_url, token=""):
        return src["client"].get("/api/memory/sync/manifest").json()

    def _fetch_blob(base_url, digest, token=""):
        r = src["client"].get(f"/api/memory/sync/blob/{digest}")
        return r.content if r.status_code == 200 else None

    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest", _fetch_manifest)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_blob", _fetch_blob)
    _activate(monkeypatch, dst)
    return loop.sync_with({"id": src["name"], "urls": ["http://stub"], "token": ""})


def test_disjoint_notes_converge_in_both_directions(monkeypatch, tmp_path):
    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _write_capture(nuc, "n-20260719-aaaaaa.md", "from nuc")
    book = _peer(monkeypatch, tmp_path, "book")
    _write_capture(book, "b-20260719-bbbbbb.md", "from book")

    _pull(monkeypatch, book, nuc)
    _pull(monkeypatch, nuc, book)

    for peer in (nuc, book):
        names = {p.name for p in (peer["home"] / "md" / "captures").glob("*.md")}
        assert names == {"n-20260719-aaaaaa.md", "b-20260719-bbbbbb.md"}


def test_a_second_cycle_changes_nothing(monkeypatch, tmp_path):
    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _write_capture(nuc, "n-20260719-aaaaaa.md", "from nuc")
    book = _peer(monkeypatch, tmp_path, "book")

    first = _pull(monkeypatch, book, nuc)
    second = _pull(monkeypatch, book, nuc)

    assert first["applied"] == 1
    assert second["applied"] == 0


def test_concurrent_edit_leaves_a_winner_and_a_sidecar(monkeypatch, tmp_path):
    def _node(peer, by: str, text: str) -> None:
        d = peer["home"] / "md" / "okf" / "global" / "learnings"
        d.mkdir(parents=True, exist_ok=True)
        (d / "L-07.md").write_text(
            f'---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 47\n'
            f'updated_by: "{by}"\n---\n\n{text}\n', encoding="utf-8")

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _node(nuc, "nuc", "nuc version")
    book = _peer(monkeypatch, tmp_path, "book")
    _node(book, "book", "book version")

    res = _pull(monkeypatch, book, nuc)

    node = book["home"] / "md" / "okf" / "global" / "learnings" / "L-07.md"
    sidecar = node.parent / "L-07.conflict.md"
    assert res["conflicts"] == 1
    # 'nuc' > 'book' lexicographically, so the remote wins on the tie.
    assert "nuc version" in node.read_text(encoding="utf-8")
    assert "book version" in sidecar.read_text(encoding="utf-8")


def test_an_unreachable_peer_is_survived(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import loop

    book = _peer(monkeypatch, tmp_path, "book")
    _activate(monkeypatch, book)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest",
                        lambda *a, **k: {})

    res = loop.sync_with({"id": "gone", "urls": ["http://127.0.0.1:1"], "token": ""})

    assert res["ok"] is False
    assert res["applied"] == 0


def test_a_tampered_blob_is_rejected(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import loop

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _write_capture(nuc, "n-20260719-aaaaaa.md", "from nuc")
    book = _peer(monkeypatch, tmp_path, "book")

    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest",
                        lambda *a, **k: nuc["client"].get(
                            "/api/memory/sync/manifest").json())
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_blob",
                        lambda *a, **k: b"TAMPERED")
    _activate(monkeypatch, book)

    res = loop.sync_with({"id": "nuc", "urls": ["http://stub"], "token": ""})

    assert res["applied"] == 0
    assert res["rejected"] == 1
    assert not (book["home"] / "md" / "captures").exists()
    assert hashlib.sha256(b"from nuc").hexdigest()   # sanity: the real hash differs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_two_peer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aiforge_core.memory.sync.loop'`

- [ ] **Step 3: Write minimal implementation**

Create `aiforge_core/memory/sync/loop.py`:

```python
"""One sync cycle, and the scheduler that repeats it.

Pull only, never push. A peer that is down is a request that returns nothing
this cycle; nothing blocks on it and nothing is queued for it. Every node
pulling from every other node is sufficient for the whole mesh to converge.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger("aiforge.sync")

DEFAULT_INTERVAL = 900  # 15 minutes


def _first_url(peer: dict) -> str:
    urls = [u for u in (peer.get("urls") or []) if u]
    return urls[0] if urls else ""


def sync_with(peer: dict) -> dict:
    """Run one cycle against a single peer.

    Returns ``{ok, applied, rejected, conflicts}``. Never raises: an unreachable
    or misbehaving peer must not take the local node down.
    """
    from aiforge_core.memory.sync import apply, manifest, merge, peers, transport

    result = {"ok": False, "applied": 0, "rejected": 0, "conflicts": 0}
    base = _first_url(peer)
    if not base:
        return result

    remote = transport.fetch_manifest(base, str(peer.get("token") or ""))
    if not remote:
        return result
    result["ok"] = True

    local = manifest.build()
    plan = merge.plan_sync(local, remote.get("manifest") or [])

    for pair in plan["conflict"]:
        if apply.keep_conflict(pair["local"]):
            result["conflicts"] += 1

    for entry in plan["want"]:
        body = transport.fetch_blob(base, str(entry.get("hash") or ""),
                                 str(peer.get("token") or ""))
        if body is None:
            result["rejected"] += 1
            continue
        if apply.apply_blob(entry, body):
            result["applied"] += 1
        else:
            result["rejected"] += 1

    peers.merge_roster(remote.get("roster") or [])
    peers.touch(str(peer.get("id") or ""))

    _log.info("sync: %s applied=%d rejected=%d conflicts=%d", peer.get("id"),
              result["applied"], result["rejected"], result["conflicts"])
    return result


def run_once() -> list[dict]:
    """One cycle across every approved peer."""
    from aiforge_core.memory.sync import peers

    out = []
    for peer in peers.approved():
        try:
            out.append({"peer": peer.get("id"), **sync_with(peer)})
        except Exception as exc:  # noqa: BLE001 — one bad peer must not stop the rest
            _log.warning("sync: cycle failed for %s: %s", peer.get("id"), exc)
    return out


def run_forever(interval: int = DEFAULT_INTERVAL) -> None:
    while True:
        run_once()
        time.sleep(interval)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="AIForge peer memory sync")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.once:
        for row in run_once():
            print(row)
        return
    run_forever(args.interval)


__all__ = ["sync_with", "run_once", "run_forever", "main"]
```

Add the CLI entry to `pyproject.toml` under `[project.scripts]`:

```toml
aiforge-sync = "aiforge_core.memory.sync.loop:main"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_two_peer.py -v`
Expected: PASS, 5 passed

Then the whole suite:

Run: `.venv/bin/pytest tests/python -q`
Expected: PASS, no regressions

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/loop.py pyproject.toml \
        tests/python/memory/sync/test_two_peer.py
git commit -m "feat(sync): pull-only sync loop with two-peer convergence tests"
```

---

# Phase 4 — Discovery

## Task 9: SSDP on the local segment

**Files:**
- Create: `aiforge_core/memory/sync/discovery_ssdp.py`
- Test: `tests/python/memory/sync/test_ssdp.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_ssdp.py`:

```python
"""SSDP message construction and parsing. No sockets are opened here."""
from __future__ import annotations

from aiforge_core.memory.sync import discovery_ssdp as ssdp


def test_search_datagram_is_a_wellformed_m_search():
    msg = ssdp.build_search().decode()

    assert msg.startswith("M-SEARCH * HTTP/1.1\r\n")
    assert "HOST: 239.255.255.250:1900\r\n" in msg
    assert 'MAN: "ssdp:discover"\r\n' in msg
    assert f"ST: {ssdp.SERVICE_TYPE}\r\n" in msg
    assert msg.endswith("\r\n\r\n")


def test_announce_carries_location_and_usn():
    msg = ssdp.build_announce("nuc", "http://10.0.1.14:8799").decode()

    assert "LOCATION: http://10.0.1.14:8799\r\n" in msg
    assert f"USN: uuid:nuc::{ssdp.SERVICE_TYPE}\r\n" in msg
    assert "CACHE-CONTROL: max-age=" in msg


def test_parse_extracts_a_roster_entry():
    raw = ssdp.build_announce("nuc", "http://10.0.1.14:8799")

    assert ssdp.parse(raw) == {"id": "nuc", "urls": ["http://10.0.1.14:8799"]}


def test_parse_ignores_other_services():
    raw = (b"NOTIFY * HTTP/1.1\r\nLOCATION: http://x\r\n"
           b"USN: uuid:foo::urn:schemas-upnp-org:device:MediaServer:1\r\n\r\n")

    assert ssdp.parse(raw) is None


def test_parse_survives_garbage():
    assert ssdp.parse(b"\x00\xff not http at all") is None
    assert ssdp.parse(b"") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_ssdp.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aiforge_core.memory.sync.discovery_ssdp'`

- [ ] **Step 3: Write minimal implementation**

Create `aiforge_core/memory/sync/discovery_ssdp.py`:

```python
"""Zero-config peer discovery on the local segment via SSDP.

Chosen over mDNS/DNS-SD because it needs no record marshalling and no library:
the payload is HTTP-shaped text over UDP multicast, and ``LOCATION`` already
carries a url.

This can only ever be a convenience. Multicast is link-local — small TTL,
dropped by routers and access points — and WireGuard is a routed L3 tunnel with
no broadcast domain, so SSDP fails between two of your own machines the moment
they talk over the tunnel. Gossip over the manifest carries the mesh; SSDP just
saves typing a seed url when two peers share a physical segment.

Discovered peers land in ``candidate`` state exactly like gossiped ones. SSDP is
unauthenticated and trivially spoofable, so it must never confer trust.
"""
from __future__ import annotations

import logging
import socket

_log = logging.getLogger("aiforge.sync")

MCAST_ADDR = "239.255.255.250"
MCAST_PORT = 1900
SERVICE_TYPE = "urn:aiforge:service:memory-sync:1"
MAX_AGE = 1800


def build_search() -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {MCAST_ADDR}:{MCAST_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {SERVICE_TYPE}\r\n"
        "\r\n"
    ).encode()


def build_announce(peer_id: str, url: str) -> bytes:
    return (
        "NOTIFY * HTTP/1.1\r\n"
        f"HOST: {MCAST_ADDR}:{MCAST_PORT}\r\n"
        f"CACHE-CONTROL: max-age={MAX_AGE}\r\n"
        f"LOCATION: {url}\r\n"
        f"NT: {SERVICE_TYPE}\r\n"
        "NTS: ssdp:alive\r\n"
        f"USN: uuid:{peer_id}::{SERVICE_TYPE}\r\n"
        "\r\n"
    ).encode()


def parse(raw: bytes) -> dict | None:
    """Extract ``{id, urls}`` from a datagram, or ``None`` if it is not ours."""
    try:
        text = raw.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 — arbitrary bytes arrive on a multicast socket
        return None
    headers: dict[str, str] = {}
    for line in text.split("\r\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().upper()] = v.strip()
    usn = headers.get("USN", "")
    if SERVICE_TYPE not in usn or SERVICE_TYPE not in text:
        return None
    location = headers.get("LOCATION", "")
    peer_id = usn.removeprefix("uuid:").split("::", 1)[0].strip()
    if not peer_id or not location:
        return None
    return {"id": peer_id, "urls": [location]}


def _socket(bind_host: str) -> socket.socket:
    """A multicast socket bound to one interface.

    Binding to a specific LAN address rather than ``0.0.0.0`` is deliberate:
    SSDP responders are a well-known DDoS amplification vector, and a responder
    reachable beyond the local segment becomes someone else's amplifier.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(bind_host))
    sock.bind((bind_host, 0))
    return sock


def discover(bind_host: str, timeout: float = 3.0) -> list[dict]:
    """Multicast a search and collect replies for ``timeout`` seconds."""
    found: dict[str, dict] = {}
    try:
        sock = _socket(bind_host)
    except OSError as exc:  # noqa: BLE001 — no multicast here is normal, not an error
        _log.info("sync: ssdp unavailable on %s: %s", bind_host, exc)
        return []
    try:
        sock.settimeout(timeout)
        sock.sendto(build_search(), (MCAST_ADDR, MCAST_PORT))
        while True:
            try:
                raw, _addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            entry = parse(raw)
            if entry:
                found[entry["id"]] = entry
    finally:
        sock.close()
    return list(found.values())


def announce(bind_host: str, peer_id: str, url: str) -> bool:
    try:
        sock = _socket(bind_host)
    except OSError as exc:  # noqa: BLE001
        _log.info("sync: ssdp announce unavailable on %s: %s", bind_host, exc)
        return False
    try:
        sock.sendto(build_announce(peer_id, url), (MCAST_ADDR, MCAST_PORT))
        return True
    finally:
        sock.close()


__all__ = ["build_search", "build_announce", "parse", "discover", "announce",
           "SERVICE_TYPE", "MCAST_ADDR", "MCAST_PORT"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_ssdp.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/discovery_ssdp.py tests/python/memory/sync/test_ssdp.py
git commit -m "feat(sync): SSDP discovery on the local segment"
```

---

## Task 10: Wire SSDP into the cycle, and prove transitive discovery quarantines

**Files:**
- Modify: `aiforge_core/memory/sync/loop.py` (`run_once`)
- Test: `tests/python/memory/sync/test_two_peer.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/python/memory/sync/test_two_peer.py`:

```python
def test_transitive_discovery_quarantines_the_third_peer(monkeypatch, tmp_path):
    """A knows B; B knows C. After one cycle A knows *of* C but never pulls it."""
    from aiforge_core.memory.sync import loop, peers

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    # nuc has alice approved, so alice appears in nuc's advertised roster.
    _activate(monkeypatch, nuc)
    peers.save({"self": {"id": "nuc", "urls": ["http://nuc"]}, "peers": [
        {"id": "alice", "urls": ["http://alice"], "token": "t",
         "state": "approved"},
    ]})

    book = _peer(monkeypatch, tmp_path, "book")
    _activate(monkeypatch, book)
    peers.save({"self": {"id": "book", "urls": ["http://book"]}, "peers": [
        {"id": "nuc", "urls": ["http://stub"], "token": "", "state": "approved"},
    ]})

    _pull(monkeypatch, book, nuc)

    _activate(monkeypatch, book)
    known = {p["id"]: p for p in peers.load()["peers"]}
    assert known["alice"]["state"] == "candidate"
    assert "token" not in known["alice"]
    assert [p["id"] for p in peers.approved()] == ["nuc"]


def test_ssdp_discoveries_are_also_quarantined(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import loop, peers

    book = _peer(monkeypatch, tmp_path, "book")
    _activate(monkeypatch, book)
    peers.save({"self": {"id": "book", "urls": ["http://book"]}, "peers": []})
    monkeypatch.setenv("AIFORGE_SYNC_SSDP", "1")
    monkeypatch.setattr("aiforge_core.memory.sync.discovery_ssdp.discover",
                        lambda *a, **k: [{"id": "nuc", "urls": ["http://found"]}])

    loop.run_once()

    known = {p["id"]: p for p in peers.load()["peers"]}
    assert known["nuc"]["state"] == "candidate"
    assert peers.approved() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_two_peer.py -v`
Expected: FAIL — `test_ssdp_discoveries_are_also_quarantined` fails; `run_once` never calls SSDP

- [ ] **Step 3: Write minimal implementation**

In `aiforge_core/memory/sync/loop.py`, add the SSDP sweep and call it from `run_once`:

```python
def _ssdp_sweep() -> None:
    """Fold any locally-announced peers into the registry as candidates.

    Off unless ``AIFORGE_SYNC_SSDP=1``: multicast is useless across WireGuard
    and the internet, so it is opt-in for operators who actually have peers on
    the same physical segment.
    """
    import os

    if os.environ.get("AIFORGE_SYNC_SSDP", "0") != "1":
        return
    from aiforge_core.memory.sync import discovery_ssdp, peers

    bind = os.environ.get("AIFORGE_SYNC_SSDP_HOST", "")
    if not bind:
        _log.info("sync: AIFORGE_SYNC_SSDP=1 but no AIFORGE_SYNC_SSDP_HOST, skipping")
        return
    try:
        found = discovery_ssdp.discover(bind)
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort by nature
        _log.info("sync: ssdp sweep failed: %s", exc)
        return
    if found:
        peers.merge_roster(found)
```

Change `run_once` to sweep first:

```python
def run_once() -> list[dict]:
    """One cycle across every approved peer."""
    from aiforge_core.memory.sync import peers

    _ssdp_sweep()
    out = []
    for peer in peers.approved():
        try:
            out.append({"peer": peer.get("id"), **sync_with(peer)})
        except Exception as exc:  # noqa: BLE001 — one bad peer must not stop the rest
            _log.warning("sync: cycle failed for %s: %s", peer.get("id"), exc)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_two_peer.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/loop.py tests/python/memory/sync/test_two_peer.py
git commit -m "feat(sync): opt-in SSDP sweep, quarantined like all discovery"
```

---

# Phase 5 — The compaction lease

## Task 11: Lease claim, renew and expiry

**Files:**
- Create: `aiforge_core/memory/sync/lease.py`
- Test: `tests/python/memory/sync/test_lease.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_lease.py`:

```python
"""The compaction lease. Deliberately not a consensus protocol."""
from __future__ import annotations

import json


def _md(monkeypatch, tmp_path, peer_id: str):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)


def test_claim_on_an_empty_mesh_writes_the_lease(monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    assert lease.claim() is True
    rec = json.loads((tmp_path / "md" / "okf" / ".lease.json").read_text())
    assert rec["holder"] == "nuc"
    assert rec["rev"] == 1
    assert rec["key"] == "__lease__"


def test_holder_is_true_only_for_the_holder(monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    lease.claim()
    assert lease.is_holder() is True

    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    assert lease.is_holder() is False


def test_a_live_lease_cannot_be_stolen(monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    lease.claim()

    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    assert lease.claim() is False


def test_an_expired_lease_is_claimable(monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    lease.claim()
    path = tmp_path / "md" / "okf" / ".lease.json"
    rec = json.loads(path.read_text())
    rec["expires_at"] = 1          # far in the past
    path.write_text(json.dumps(rec), encoding="utf-8")

    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    assert lease.claim() is True
    assert json.loads(path.read_text())["holder"] == "book"


def test_claiming_bumps_rev_so_the_lease_merges_like_any_class_b_record(
        monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    lease.claim()
    path = tmp_path / "md" / "okf" / ".lease.json"
    rec = json.loads(path.read_text())
    rec["expires_at"] = 1
    path.write_text(json.dumps(rec), encoding="utf-8")

    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    lease.claim()

    assert json.loads(path.read_text())["rev"] == 2


def test_renew_extends_only_for_the_holder(monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    lease.claim()
    before = json.loads((tmp_path / "md" / "okf" / ".lease.json").read_text())
    assert lease.renew() is True
    after = json.loads((tmp_path / "md" / "okf" / ".lease.json").read_text())
    assert after["expires_at"] >= before["expires_at"]

    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    assert lease.renew() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_lease.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aiforge_core.memory.sync.lease'`

- [ ] **Step 3: Write minimal implementation**

Create `aiforge_core/memory/sync/lease.py`:

```python
"""The compaction lease — the only part of sync that needs a leader.

Replication needs no leader at all. Compaction, OKF node deduplication and
distillation do, because they are LLM-expensive and non-deterministic: two peers
running them concurrently produce different answers from the same input.

This is deliberately not a consensus protocol. If two peers both believe they
hold the lease, both compact; both briefs are content-addressed class A files,
so both land and the next concept-similarity dedupe pass merges them. The cost
of split-brain is wasted tokens, never corruption — which is exactly why Raft
would be more code than the entire rest of this design.

Claim protocol: write the lease with ``rev + 1``, wait one full sync interval,
then read it back. Still holding it? You are the leader. That wait is what
replaces consensus.
"""
from __future__ import annotations

import logging
import time

from aiforge_core.memory.sync import _io, paths
from aiforge_core.memory.sync.paths import LEASE_KEY

_log = logging.getLogger("aiforge.sync")

TTL = 600          # 10 minutes
RENEW_EVERY = 180  # 3 minutes


def read() -> dict:
    """The current lease record, or {} if there is none."""
    return _io.read_json(paths.lease_path())


def _write(rec: dict) -> None:
    _io.write_json(paths.lease_path(), rec)


def _expired(rec: dict) -> bool:
    return int(rec.get("expires_at") or 0) <= int(time.time())


def claim() -> bool:
    """Take the lease if it is free or expired. Returns whether we now hold it."""
    from aiforge_core.memory.sync.identity import self_id

    me = self_id()
    rec = read()
    if rec and not _expired(rec) and rec.get("holder") != me:
        return False
    now = int(time.time())
    _write({
        "origin": "",
        "key": LEASE_KEY,
        "rev": int(rec.get("rev") or 0) + 1,
        "updated_by": me,
        "holder": me,
        "expires_at": now + TTL,
    })
    _log.info("sync: claimed compaction lease as %s", me)
    return True


def renew() -> bool:
    """Extend our own lease. Returns False if we are not the holder."""
    from aiforge_core.memory.sync.identity import self_id

    me = self_id()
    rec = read()
    if rec.get("holder") != me:
        return False
    rec["expires_at"] = int(time.time()) + TTL
    rec["rev"] = int(rec.get("rev") or 0) + 1
    rec["updated_by"] = me
    _write(rec)
    return True


def is_holder() -> bool:
    from aiforge_core.memory.sync.identity import self_id

    rec = read()
    return bool(rec) and not _expired(rec) and rec.get("holder") == self_id()


__all__ = ["claim", "renew", "is_holder", "read", "TTL", "RENEW_EVERY", "LEASE_KEY"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_lease.py -v`
Expected: PASS, 6 passed

Then the whole suite:

Run: `.venv/bin/pytest tests/python -q`
Expected: PASS, no regressions

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/lease.py tests/python/memory/sync/test_lease.py
git commit -m "feat(sync): TTL-based compaction lease, split-brain tolerant"
```

---

## Task 12: Deleting a node writes a tombstone

Nothing so far *creates* a tombstone — the client only applies ones fetched from a peer. A
local delete that just unlinks the file would be undone by the next pull.

**Files:**
- Create: `aiforge_core/memory/sync/tombstone.py`
- Test: `tests/python/memory/sync/test_tombstone.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_tombstone.py`:

```python
"""Local deletion must be expressible to the mesh, not just to the filesystem."""
from __future__ import annotations

import json


def _env(monkeypatch, tmp_path, peer_id: str = "book"):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)


def _node(tmp_path, origin: str, key: str, rev: int):
    p = tmp_path / "md" / "okf" / "global" / "learnings" / f"{key}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\ntype: learning\nid: "{key}"\norigin: "{origin}"\n'
                 f'rev: {rev}\nupdated_by: "{origin}"\n---\n\nbody\n',
                 encoding="utf-8")
    return p


def test_delete_removes_the_node_and_leaves_a_tombstone(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import tombstone

    node = _node(tmp_path, "nuc", "L-07", 47)

    assert tombstone.delete_node("nuc", "L-07") is True
    assert not node.exists()

    rec = json.loads((tmp_path / "md" / "okf" / ".tomb" / "nuc" / "L-07.json")
                     .read_text(encoding="utf-8"))
    assert rec == {"origin": "nuc", "key": "L-07", "rev": 48,
                   "updated_by": "book", "tomb": True}


def test_tombstone_rev_beats_the_node_it_replaced(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import merge, tombstone

    _node(tmp_path, "nuc", "L-07", 47)
    tombstone.delete_node("nuc", "L-07")

    from aiforge_core.memory.sync import manifest
    local = [{"path": "x", "hash": "h", "cls": "B", "origin": "nuc",
              "key": "L-07", "rev": 47, "updated_by": "nuc"}]
    remote = manifest.build()

    # A peer still holding rev 47 must accept the tombstone.
    assert merge.plan_sync(local, remote)["want"][0]["tomb"] is True


def test_deleting_an_unknown_identity_is_a_no_op(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import tombstone

    assert tombstone.delete_node("nuc", "L-99") is False
    assert not (tmp_path / "md" / "okf" / ".tomb").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/python/memory/sync/test_tombstone.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aiforge_core.memory.sync.tombstone'`

- [ ] **Step 3: Write minimal implementation**

Create `aiforge_core/memory/sync/tombstone.py`:

```python
"""Deletion, expressed so the mesh can hear it.

A grow-only set cannot say "removed" — unlinking a file locally is undone by
the next pull. A tombstone is a class B record carrying the identity and a
revision one higher than the node it replaces, so it beats the version every
other peer is still holding, and a genuinely newer edit later beats it back.
"""
from __future__ import annotations

import logging

from aiforge_core.memory.sync import _io, paths

_log = logging.getLogger("aiforge.sync")


def delete_node(origin: str, key: str) -> bool:
    """Remove a node and record a tombstone. False if no such identity exists."""
    from aiforge_core.memory.okf import nodes as _nodes
    from aiforge_core.memory.sync.identity import self_id

    found = paths.node_paths(origin, key)
    if not found:
        return False

    rev = 0
    for p in found:
        try:
            meta = (_nodes.parse_node(p.read_text(encoding="utf-8")).get("meta") or {})
            rev = max(rev, int(meta.get("rev") or 0))
        except Exception:  # noqa: BLE001 — an unreadable node is still deletable
            continue

    for p in found:
        p.unlink(missing_ok=True)

    _io.write_json(paths.tomb_path(origin, key),
                   {"origin": origin, "key": key, "rev": rev + 1,
                    "updated_by": self_id(), "tomb": True})
    _log.info("sync: tombstoned (%s, %s) at rev %d", origin, key, rev + 1)
    return True


__all__ = ["delete_node"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/python/memory/sync/test_tombstone.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/tombstone.py tests/python/memory/sync/test_tombstone.py
git commit -m "feat(sync): local deletion writes a tombstone the mesh can merge"
```

---

## Task 13: Lint and full-suite gate

**Files:**
- Modify: whatever ruff flags

- [ ] **Step 1: Run the linter**

Run: `.venv/bin/ruff check aiforge_core/memory/sync aiforge_core/api/routes/sync.py tests/python/memory/sync`
Expected: `All checks passed!`

- [ ] **Step 2: Fix anything it flags**

Common ones in this codebase: `I001` import ordering (run `.venv/bin/ruff check --fix`), `E501`
lines over 100 characters, `SIM108` ternary suggestions. Apply the fix, do not add `noqa`
unless the suppression is genuinely justified — and if it is, add the explanatory comment the
house style requires.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/pytest tests/python -q`
Expected: PASS, no failures, no new warnings

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore(sync): lint clean"
```

---

# Phase 6 — Live two-machine validation

Everything so far is in-process. This phase proves it on real hardware across a real network:
`nuc` (192.168.70.115, `aiforge-api` on `0.0.0.0:8799`) and `book` (this Mac, 192.168.70.227).
Both are on one subnet, so SSDP is exercisable for real.

**These steps push a branch and restart a service on the NUC. Confirm with the user before
running Task 14 Step 1.**

## Task 14: Deploy the branch to the NUC

- [ ] **Step 1: Push the branch (requires user confirmation)**

```bash
git push -u origin feat/p2p-shared-memory
```

- [ ] **Step 2: Fetch and check out the branch on the NUC**

```bash
ssh ai@192.168.70.115 'cd ~/AIForgeCrew && git fetch origin && git checkout feat/p2p-shared-memory && git pull --ff-only origin feat/p2p-shared-memory && git log --oneline -1'
```
Expected: the branch HEAD commit hash, matching local.

- [ ] **Step 3: Restart the API and confirm the routes exist**

```bash
ssh ai@192.168.70.115 'systemctl --user restart aiforge-api && sleep 5 && systemctl --user is-active aiforge-api'
```
Expected: `active`

```bash
ssh ai@192.168.70.115 'curl -sS -o /dev/null -w "%{http_code}\n" localhost:8799/api/memory/sync/manifest'
```
Expected: `200` (or `401` if `AIFORGE_API_TOKEN` is set on the NUC — either proves the route is
mounted; a `404` means it is not).

## Task 15: Live convergence between the two machines

- [ ] **Step 1: Configure identity on both peers**

On the NUC:

```bash
ssh ai@192.168.70.115 'python3 - <<PY
import json, os, pathlib
d = pathlib.Path(os.path.expanduser("~/.aiforge")); d.mkdir(parents=True, exist_ok=True)
(d / "peers.json").write_text(json.dumps({
  "self": {"id": "nuc", "urls": ["http://192.168.70.115:8799"]},
  "peers": [{"id": "book", "urls": ["http://192.168.70.227:8799"],
             "token": "", "state": "approved"}]
}, indent=2))
print((d / "peers.json").read_text())
PY'
```

On this Mac, write the mirror image:

```bash
python3 - <<'PY'
import json, os, pathlib
d = pathlib.Path(os.path.expanduser("~/.aiforge")); d.mkdir(parents=True, exist_ok=True)
(d / "peers.json").write_text(json.dumps({
  "self": {"id": "book", "urls": ["http://192.168.70.227:8799"]},
  "peers": [{"id": "nuc", "urls": ["http://192.168.70.115:8799"],
             "token": "", "state": "approved"}]
}, indent=2))
print((d / "peers.json").read_text())
PY
```

Also set `AIFORGE_PEER_ID=nuc` on the NUC service environment and `AIFORGE_PEER_ID=book`
locally, so the id does not fall back to the hostname slug.

- [ ] **Step 2: Start the API locally, bound so the NUC can reach it**

```bash
AIFORGE_PEER_ID=book AIFORGE_BIND_HOST=0.0.0.0 AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1 \
  .venv/bin/uvicorn aiforge_core.api.api:app --host 0.0.0.0 --port 8799 &
sleep 4
curl -sS localhost:8799/api/memory/sync/manifest | head -c 200
```
Expected: JSON with `manifest` and `roster` keys.

Note: `AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1` is required because `_security_boot_guard()`
(`aiforge_core/api/api.py:521`) refuses a non-loopback bind without a token. For a LAN test
this is acceptable; for anything beyond it, set `AIFORGE_API_TOKEN` on both peers and put the
same value in each `peers.json` entry instead.

- [ ] **Step 3: Confirm mutual reachability**

```bash
curl -sS -o /dev/null -w "mac->nuc %{http_code}\n" http://192.168.70.115:8799/api/memory/sync/manifest
ssh ai@192.168.70.115 'curl -sS -o /dev/null -w "nuc->mac %{http_code}\n" http://192.168.70.227:8799/api/memory/sync/manifest'
```
Expected: both `200`. If `nuc->mac` fails, the Mac firewall is blocking 8799 — allow it in
System Settings → Network → Firewall, or run the test with the Mac as puller only.

- [ ] **Step 4: Seed a distinct capture on each machine**

```bash
ssh ai@192.168.70.115 'mkdir -p ~/.aiforge/memory/captures && printf -- "---\ntitle: from nuc\nkind: note\n---\n\nnuc side fact\n" > ~/.aiforge/memory/captures/live-nuc-20260719-n00001.md'
mkdir -p ~/.aiforge/memory/captures
printf -- "---\ntitle: from book\nkind: note\n---\n\nbook side fact\n" > ~/.aiforge/memory/captures/live-book-20260719-b00001.md
```

- [ ] **Step 5: Run one cycle on each peer**

```bash
.venv/bin/python -m aiforge_core.memory.sync.loop --once
ssh ai@192.168.70.115 'cd ~/AIForgeCrew && .venv/bin/python -m aiforge_core.memory.sync.loop --once'
```
Expected on each: a line like `{'peer': 'nuc', 'ok': True, 'applied': 1, 'rejected': 0, 'conflicts': 0}`

- [ ] **Step 6: Assert convergence**

```bash
echo "--- book:"; ls ~/.aiforge/memory/captures/ | grep live-
echo "--- nuc:";  ssh ai@192.168.70.115 'ls ~/.aiforge/memory/captures/ | grep live-'
```
Expected: **both** listings contain `live-nuc-20260719-n00001.md` *and*
`live-book-20260719-b00001.md`.

- [ ] **Step 7: Assert idempotence**

```bash
.venv/bin/python -m aiforge_core.memory.sync.loop --once
```
Expected: `'applied': 0` — a second cycle changes nothing.

- [ ] **Step 8: Exercise a real conflict**

Write the same OKF node on both machines at the same `rev` with different bodies:

```bash
ssh ai@192.168.70.115 'mkdir -p ~/.aiforge/memory/okf/global/learnings && printf -- "---\ntype: learning\nid: \"L-99\"\norigin: \"nuc\"\nrev: 5\nupdated_by: \"nuc\"\n---\n\nnuc body\n" > ~/.aiforge/memory/okf/global/learnings/L-99.md'
mkdir -p ~/.aiforge/memory/okf/global/learnings
printf -- "---\ntype: learning\nid: \"L-99\"\norigin: \"nuc\"\nrev: 5\nupdated_by: \"book\"\n---\n\nbook body\n" > ~/.aiforge/memory/okf/global/learnings/L-99.md
.venv/bin/python -m aiforge_core.memory.sync.loop --once
```
Expected: `'conflicts': 1`. Then:

```bash
cat ~/.aiforge/memory/okf/global/learnings/L-99.md          # winner: "nuc body"
cat ~/.aiforge/memory/okf/global/learnings/L-99.conflict.md # loser: "book body"
```
`nuc` > `book` lexicographically, so the remote wins the tie and the local text survives in
the sidecar.

- [ ] **Step 9: Exercise SSDP on the shared segment**

```bash
AIFORGE_SYNC_SSDP=1 AIFORGE_SYNC_SSDP_HOST=192.168.70.227 \
  .venv/bin/python -c "from aiforge_core.memory.sync import discovery_ssdp as s; print(s.discover('192.168.70.227'))"
```

With an announcer running on the NUC in another shell:

```bash
ssh ai@192.168.70.115 'cd ~/AIForgeCrew && .venv/bin/python -c "from aiforge_core.memory.sync import discovery_ssdp as s; print(s.announce(\"192.168.70.115\", \"nuc\", \"http://192.168.70.115:8799\"))"'
```
Expected: the Mac's `discover()` returns `[{'id': 'nuc', 'urls': ['http://192.168.70.115:8799']}]`.

If it returns `[]`, that is a *valid* outcome worth recording — it means this segment filters
multicast, which is precisely why gossip and not SSDP carries the mesh.

- [ ] **Step 10: Assert a discovered peer stayed quarantined**

```bash
python3 -c "import json,os;print([(p['id'],p['state']) for p in json.load(open(os.path.expanduser('~/.aiforge/peers.json')))['peers']])"
```
Expected: any peer learned via gossip or SSDP shows `candidate`, and only the hand-configured
one shows `approved`.

- [ ] **Step 11: Tear down the test data**

```bash
rm -f ~/.aiforge/memory/captures/live-*.md \
      ~/.aiforge/memory/okf/global/learnings/L-99.md \
      ~/.aiforge/memory/okf/global/learnings/L-99.conflict.md
ssh ai@192.168.70.115 'rm -f ~/.aiforge/memory/captures/live-*.md ~/.aiforge/memory/okf/global/learnings/L-99.md'
kill %1   # the local uvicorn started in Step 2
```

- [ ] **Step 12: Record the outcome**

Append a short results section to
`docs/superpowers/specs/2026-07-19-p2p-shared-memory-design.md` noting: whether convergence
held both directions, whether SSDP worked on this segment, and any firewall or bind caveat
found. Commit.

```bash
git add docs/superpowers/specs/2026-07-19-p2p-shared-memory-design.md
git commit -m "docs(spec): record live two-machine validation results"
```

---

## Not built (deliberately)

Per the spec's out-of-scope section: chat sessions, tickets, pipeline state, repo indexes,
media, and vectors. Vectors are recomputed locally and never transferred, because peers may run
different embedding backends and a transferred vector could be the wrong dimension.
`memory.db` never crosses the wire.

Also deferred: ed25519 signed manifests (self-certifying roster entries), Merkle-digest
manifests (only worth it above a few thousand entries), and tombstone/sidecar reaping at 90
days — the reaper is a scheduled job, and none of the above changes the protocol.
