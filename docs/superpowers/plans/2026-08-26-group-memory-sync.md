# Group-scoped Memory Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One admin hub serves several independent client fleets ("groups"), each client filters credentials and idle-search noise out of what it sends, neither side's authored `okf/` tree can be written by sync, and both sides can revert to a recent snapshot.

**Architecture:** A group scopes the whole memory tree on the admin via a contextvar override on `_io.root()` — every downstream module (`paths`, `manifest`, `merge`, `apply`, `inbox`, `tiers`) is already written against that one function and needs no change. The client stays unscoped: it belongs to one group and simply *sends* the name. A new `sync/redact/` package is the client's outbound gate, running deterministically inside `push._mine`. Snapshots are hardlink copies under a dotted directory that the manifest scanner already excludes.

**Tech Stack:** Python 3.12, FastAPI, pytest, React 18 + Vite (TypeScript), bash (`run.sh`).

**Spec:** `docs/superpowers/specs/2026-08-26-group-memory-sync-design.md`

**Worktree:** `.worktrees/feat/group-memory-sync`, branch `feat/group-memory-sync`. All paths below are relative to that worktree.

---

## File Structure

**New files:**

| Path | Responsibility |
|---|---|
| `aiforge_core/memory/sync/group.py` | Group names: validation, the admin's list, the client's selection, the `scoped()` context manager |
| `aiforge_core/memory/sync/status.py` | The one status record, and the state-change logger that keeps a down admin quiet |
| `aiforge_core/memory/sync/snapshot.py` | Hardlink snapshots, listing, pruning and revert |
| `aiforge_core/memory/sync/redact/__init__.py` | The filter's public API: `review()`, `explain()`, `Verdict` |
| `aiforge_core/memory/sync/redact/secrets.py` | Credential detection |
| `aiforge_core/memory/sync/redact/private.py` | Personal/local-scope detection |
| `aiforge_core/memory/sync/redact/noise.py` | Idle-search / low-substance detection |
| `aiforge_core/memory/sync/redact/_text.py` | Shared node→text extraction and substance measurement |
| `aiforge_core/api/routes/groups.py` | `GET /api/memory/sync/groups`, `POST /api/admin/groups`, snapshot/revert routes |
| `web/src/views/Home.MemorySyncCard.tsx` | Settings panel |
| `tests/python/memory/sync/test_group.py` | Group name rules, list, selection order |
| `tests/python/memory/sync/test_group_isolation.py` | Two groups, two clients, no crossover |
| `tests/python/memory/sync/test_redact.py` | Every filter rule, both directions |
| `tests/python/memory/sync/test_untouchable_okf.py` | Sync can never write an authored tree |
| `tests/python/memory/sync/test_snapshot.py` | Snapshot, prune, revert, revert-the-revert |
| `tests/python/memory/sync/test_status_quiet.py` | Status record + one warning, not N |
| `tests/python/test_run_sh_group_flags.py` | `run.sh --admin-url` / `--group` |

**Modified files:**

| Path | Change |
|---|---|
| `aiforge_core/memory/sync/_io.py` | `root()` consults a contextvar override; new `assert_not_ours()` |
| `aiforge_core/memory/sync/apply.py` | `apply_blob` calls `assert_not_ours` before writing |
| `aiforge_core/memory/sync/push.py` | `_mine` runs the filter; `run_once` takes a group |
| `aiforge_core/memory/sync/transport.py` | Group rides every call; failures go through the quiet logger |
| `aiforge_core/memory/sync/loop.py` | Resolve the group, halt on `needs-group-selection`, write status |
| `aiforge_core/memory/sync/inbox.py` | `accept` re-runs the filter |
| `aiforge_core/api/routes/sync.py` | Four routes enter the caller's group scope |
| `aiforge_core/api/routes/admin.py` | Report group + status |
| `aiforge_core/api/api.py` | Register `routes/groups.py` |
| `aiforge_core/memory/okf/tiers.py` | Fold once per group; `build_view` builds into `view.tmp/` and swaps |
| `run.sh` | `--admin-url`, `--group`, banner line |
| `web/src/views/Home.tsx` | Render `<MemorySyncCard />` |

---

## Task 1: A scoped memory root, and an authored tree sync cannot touch

`_io.root()` is the single function every sync module asks "where is the memory
tree". Making it overridable per-task is what buys group isolation for free
everywhere downstream. `assert_not_ours()` is the enforced version of the rule
`paths._is_ours` currently only *prefers*.

**Files:**
- Modify: `aiforge_core/memory/sync/_io.py`
- Test: `tests/python/memory/sync/test_io.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/python/memory/sync/test_io.py`:

```python
def test_scope_override_wins_over_env(tmp_path, monkeypatch):
    """A scope repoints the tree without touching the process-wide env."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "base"))
    from aiforge_core.memory.sync import _io

    outer = _io.root()
    token = _io.push_scope(tmp_path / "base" / "groups" / "cellular")
    try:
        assert _io.root() == tmp_path / "base" / "groups" / "cellular"
    finally:
        _io.pop_scope(token)
    assert _io.root() == outer


def test_scope_is_not_visible_to_another_context(tmp_path, monkeypatch):
    """Two concurrent tasks in different groups do not see each other's root."""
    import asyncio

    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "base"))
    from aiforge_core.memory.sync import _io

    seen: dict[str, object] = {}

    async def _task(name):
        token = _io.push_scope(tmp_path / "base" / "groups" / name)
        try:
            await asyncio.sleep(0)          # yield, so the tasks interleave
            seen[name] = _io.root()
        finally:
            _io.pop_scope(token)

    async def _both():
        await asyncio.gather(_task("a"), _task("b"))

    asyncio.run(_both())
    assert seen["a"] == tmp_path / "base" / "groups" / "a"
    assert seen["b"] == tmp_path / "base" / "groups" / "b"


def test_assert_not_ours_refuses_a_write_into_okf(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    with pytest.raises(_io.AuthoredTreeError):
        _io.assert_not_ours(_io.root() / "okf" / "O-01.md")


def test_assert_not_ours_allows_a_tombstone(tmp_path, monkeypatch):
    """okf/.tomb/ is the one legitimate network-driven write below okf/."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    _io.assert_not_ours(_io.root() / "okf" / ".tomb" / "ms" / "O-01.json")


def test_assert_not_ours_allows_peers_and_mesh(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    _io.assert_not_ours(_io.root() / "peers" / "ms" / "O-01.md")
    _io.assert_not_ours(_io.root() / "mesh" / "nuc" / "M-01.md")


def test_assert_not_ours_follows_the_scope(tmp_path, monkeypatch):
    """Inside a group scope the protected tree is THAT group's okf/."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    group = tmp_path / "md" / "groups" / "cellular"
    token = _io.push_scope(group)
    try:
        with pytest.raises(_io.AuthoredTreeError):
            _io.assert_not_ours(group / "okf" / "O-01.md")
        # the unscoped tree is not what this scope protects
        _io.assert_not_ours(tmp_path / "md" / "peers" / "ms" / "O-01.md")
    finally:
        _io.pop_scope(token)
```

Add `import pytest` at the top of the file if it is not already there.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_io.py -v -k "scope or not_ours"
```

Expected: FAIL — `AttributeError: module 'aiforge_core.memory.sync._io' has no attribute 'push_scope'`.

- [ ] **Step 3: Implement**

In `aiforge_core/memory/sync/_io.py`, add below the `_ROOTS` declaration:

```python
# The group scope, if one is active. A ContextVar rather than an env var: the
# API serves requests concurrently, and AIFORGE_MEMORY_MD_DIR is process-global,
# so two clients in different groups would race and one would write into the
# other's tree. A ContextVar is per-task by construction, and an `await` inside
# a scoped handler cannot leak it to a handler serving somebody else.
_SCOPE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "aiforge_sync_root_scope", default=None)


class AuthoredTreeError(Exception):
    """A write was aimed at a tree whose only writer is the machine itself.

    Raised rather than returned: every caller is a per-record applier that
    already refuses a record by exception or by False, and a silent skip here
    would be indistinguishable from a successful write in the counters.
    """


def push_scope(directory: Path):
    """Point ``root()`` at ``directory`` for this task. Returns a reset token."""
    return _SCOPE.set(Path(directory))


def pop_scope(token) -> None:
    _SCOPE.reset(token)
```

Add `import contextvars` to the imports.

Change `root()` so the override is consulted **before** the cache — the cache is
keyed on the selecting env, which knows nothing about a scope:

```python
def root() -> Path:
    """The markdown memory tree — the source of truth this whole feature syncs.

    A group scope wins when one is active (``push_scope``): on the admin every
    group is a separate tree, and every module below this one addresses it
    through this function alone, so scoping here is what scopes all of them.

    Otherwise: cached per selecting-env, not per process — tests (and a future
    multi-tree host) swap ``AIFORGE_MEMORY_MD_DIR`` between calls, and a flat
    module-level cache would serve one peer's tree to another. The cached value
    is only the resolved path — every writer still mkdirs its own parents — so a
    root deleted underneath us costs nothing.
    """
    scoped = _SCOPE.get()
    if scoped is not None:
        return scoped

    from aiforge_core.memory.md_store import memory_dir

    key = (os.environ.get("AIFORGE_MEMORY_MD_DIR") or "",
           os.environ.get("AIFORGE_CONFIG_DIR") or "")
    cached = _ROOTS.get(key)
    if cached is None:
        cached = _ROOTS[key] = memory_dir()
    return cached
```

Add `assert_not_ours` after `safe_target`:

```python
# The one directory below ``okf/`` a record arriving over the network may
# legitimately create. A tombstone is already guarded to self-origin by
# ``apply._accept_class_b``, and it is how a deletion propagates at all.
_TOMB = ".tomb"


def assert_not_ours(target: Path) -> None:
    """Raise ``AuthoredTreeError`` if ``target`` lies inside the authored tree.

    ``okf/`` has exactly one writer: the machine it belongs to. Corrupting it is
    the failure this whole feature must be structurally incapable of, on the
    client (its own notes) and on the admin (its own notes, and each group's).

    ``paths.target_for`` already routes away from ``okf/`` — this is the
    enforced half of the same rule, checked at the point of the write rather
    than at the point of the decision, so a future routing bug can only ever
    cost a refused record instead of reaching an authored note.

    Scope-aware by construction: it asks ``root()``, so inside a group scope the
    protected tree is that group's ``okf/``.
    """
    okf = (root() / "okf").resolve()
    try:
        resolved = Path(target).resolve()
    except (OSError, ValueError):
        # A path that cannot even be resolved is not one we are about to write.
        return
    if resolved != okf and okf not in resolved.parents:
        return
    if _TOMB in resolved.relative_to(okf).parts:
        return
    raise AuthoredTreeError(f"refusing to write inside the authored tree: {target}")
```

Extend `__all__` with `"push_scope"`, `"pop_scope"`, `"assert_not_ours"`,
`"AuthoredTreeError"`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_io.py -v
```

Expected: PASS, and every pre-existing test in the file still passes.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/_io.py tests/python/memory/sync/test_io.py
git commit -m "feat(sync): a scoped memory root, and an authored tree sync cannot write"
```

---

## Task 2: Refuse any applied record aimed at the authored tree

Task 1 built the guard. This wires it into the two places a record arrives from
the network, so the destination set becomes provably `{peers/, mesh/, okf/.tomb/}`.

**Files:**
- Modify: `aiforge_core/memory/sync/apply.py:82-110` (`apply_blob`)
- Modify: `aiforge_core/memory/sync/loop.py` (`_apply_one` already catches OSError; add the new error)
- Modify: `aiforge_core/memory/sync/inbox.py` (`accept` likewise)
- Test: `tests/python/memory/sync/test_untouchable_okf.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_untouchable_okf.py`:

```python
"""Sync can never write into an authored ``okf/`` tree.

The client's notes and the admin's notes are the two things this feature must
not be able to corrupt, so the rule is enforced at the write, not merely
preferred at the routing decision (``paths.target_for``).
"""
from __future__ import annotations

import hashlib

import pytest

from aiforge_core.memory.sync import _io, apply, inbox


def _node_bytes(origin: str, key: str, rev: int = 1) -> bytes:
    return (f"---\norigin: {origin}\nkey: {key}\nrev: {rev}\n---\n\nbody\n"
            ).encode()


def _entry(origin: str, key: str, body: bytes, path: str) -> dict:
    return {"kind": "B", "origin": origin, "key": key, "rev": 1,
            "hash": hashlib.sha256(body).hexdigest(), "path": path}


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "me")
    root = _io.root()
    (root / "okf").mkdir(parents=True, exist_ok=True)
    return root


def test_an_entry_aimed_at_okf_is_refused_not_written(tree, monkeypatch):
    body = _node_bytes("ms", "O-01")
    entry = _entry("ms", "O-01", body, "okf/O-01.md")
    # Force the routing decision to the authored tree — the guard must catch it
    # even when target_for is wrong, which is the whole point of a second check.
    monkeypatch.setattr("aiforge_core.memory.sync.paths.target_for",
                        lambda e: tree / "okf" / "O-01.md")

    assert apply.apply_blob(entry, body, peer_id="ms") is False
    assert not (tree / "okf" / "O-01.md").exists()


def test_a_pushed_entry_aimed_at_okf_is_refused(tree, monkeypatch):
    body = _node_bytes("ms", "O-02")
    entry = _entry("ms", "O-02", body, "okf/O-02.md")
    monkeypatch.setattr("aiforge_core.memory.sync.paths.target_for",
                        lambda e: tree / "okf" / "O-02.md")

    assert inbox.accept("ms", entry, body) is False
    assert not (tree / "okf" / "O-02.md").exists()


def test_a_full_cycle_leaves_okf_untouched(tmp_path, monkeypatch):
    """End to end: an admin and a spoke sync, and neither okf/ is written."""
    from tests.python.memory.sync import _hub

    admin = _hub.node(monkeypatch, tmp_path, "nuc")
    spoke = _hub.node(monkeypatch, tmp_path, "ms", admin_url="http://admin")

    _hub.activate(monkeypatch, spoke)
    okf = _io.root() / "okf"
    okf.mkdir(parents=True, exist_ok=True)
    note = okf / "O-01.md"
    note.write_bytes(_node_bytes("ms", "O-01"))
    before = note.read_bytes(), note.stat().st_mtime_ns

    _hub.run_cycle(monkeypatch, spoke, admin)

    assert (note.read_bytes(), note.stat().st_mtime_ns) == before
```

`_hub.run_cycle` is added in Task 6 — until then this last test will error on
the import. Mark it `@pytest.mark.xfail(reason="needs _hub.run_cycle, Task 6")`
and remove the marker in Task 6.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_untouchable_okf.py -v
```

Expected: the first two FAIL — the node is written and `apply_blob` returns True.

- [ ] **Step 3: Implement**

In `apply.py`, replace the write in `apply_blob`:

```python
    try:
        _io.assert_not_ours(target)
    except _io.AuthoredTreeError as exc:
        # target_for already routes away from okf/; this is the enforced half of
        # the same rule. Reaching it means the routing was wrong, so the record
        # is refused and counted rather than written.
        _log.warning("sync: %s", exc)
        return False

    _io.write_atomic(target, body)
```

In `loop.py`, widen `_apply_one`'s except clause — a guarded refusal must cost
one record, never the cycle:

```python
    try:
        return apply.apply_blob(entry, body, peer_id=admin)
    except (OSError, _io.AuthoredTreeError) as exc:
```

and add `from aiforge_core.memory.sync import _io` to that function's local
imports (the module imports lazily inside functions — follow that pattern).

In `inbox.accept`, the same:

```python
    try:
        return apply.apply_blob(entry, body, peer_id=peer_id)
    except (OSError, _io.AuthoredTreeError) as exc:
```

with `_io` added to the local import line.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_untouchable_okf.py tests/python/memory/sync/test_apply.py tests/python/memory/sync/test_hostile_peer.py -v
```

Expected: PASS (the cycle test xfails until Task 6).

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/apply.py aiforge_core/memory/sync/loop.py \
        aiforge_core/memory/sync/inbox.py tests/python/memory/sync/test_untouchable_okf.py
git commit -m "feat(sync): refuse any applied record aimed at the authored tree"
```

---

## Task 3: Group names, the admin's list, and the scope

**Files:**
- Create: `aiforge_core/memory/sync/group.py`
- Test: `tests/python/memory/sync/test_group.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/python/memory/sync/test_group.py`:

```python
"""Group names, who owns the list, and how a client picks one.

The admin publishes; the client selects. A client naming its own group was
rejected in the design: a typo silently creates a second pool that looks like a
working sync until somebody asks why two machines cannot see each other.
"""
from __future__ import annotations

import pytest

from aiforge_core.memory.sync import group


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    for k in ("AIFORGE_SYNC_GROUP", "AIFORGE_SYNC_GROUPS", "AIFORGE_ADMIN_URL"):
        monkeypatch.delenv(k, raising=False)


# ── names ────────────────────────────────────────────────────────────────

def test_a_name_that_does_not_round_trip_is_refused():
    """A group name becomes a directory component, so it takes the identity
    alphabet — refused at creation, never repaired into something else."""
    assert group.is_valid("cellular")
    assert group.is_valid("site-2_north")
    assert not group.is_valid("../etc")
    assert not group.is_valid("a b")
    assert not group.is_valid("")
    assert not group.is_valid("x" * 200)


def test_create_refuses_an_invalid_name():
    with pytest.raises(ValueError):
        group.create("../etc")


# ── the admin's list ─────────────────────────────────────────────────────

def test_no_configuration_means_ungrouped():
    """An install that predates this feature keeps working with no migration."""
    assert group.known() == []


def test_the_list_is_seeded_from_the_env_once(monkeypatch):
    monkeypatch.setenv("AIFORGE_SYNC_GROUPS", "cellular, retail")
    assert group.known() == ["cellular", "retail"]


def test_create_persists_and_is_idempotent():
    group.create("cellular")
    group.create("cellular")
    assert group.known() == ["cellular"]


def test_an_env_seed_does_not_overwrite_a_created_list(monkeypatch):
    group.create("cellular")
    monkeypatch.setenv("AIFORGE_SYNC_GROUPS", "retail")
    assert group.known() == ["cellular"]


# ── the client's selection ───────────────────────────────────────────────

def test_env_pins_the_group_and_discovery_is_not_consulted(monkeypatch):
    monkeypatch.setenv("AIFORGE_SYNC_GROUP", "cellular")
    assert group.resolve(["retail", "other"]) == ("cellular", "ok")


def test_exactly_one_advertised_group_is_auto_selected_and_persisted():
    assert group.resolve(["cellular"]) == ("cellular", "ok")
    assert group.selected() == "cellular"


def test_several_advertised_and_none_chosen_halts():
    assert group.resolve(["cellular", "retail"]) == ("", "needs-group-selection")
    assert group.selected() == ""


def test_a_chosen_group_survives_a_later_ambiguity():
    group.choose("cellular")
    assert group.resolve(["cellular", "retail"]) == ("cellular", "ok")


def test_a_chosen_group_that_vanishes_is_kept_and_reported():
    """Clearing it would re-run auto-select and move this machine's knowledge
    into a different pool because somebody was mid-edit on the admin."""
    group.choose("cellular")
    assert group.resolve(["retail"]) == ("cellular", "group-unknown")
    assert group.selected() == "cellular"


def test_an_admin_advertising_none_is_ungrouped():
    assert group.resolve([]) == ("", "ok")


def test_choose_refuses_an_invalid_name():
    with pytest.raises(ValueError):
        group.choose("../etc")


# ── the scope ────────────────────────────────────────────────────────────

def test_scoped_repoints_the_tree_and_restores_it(tmp_path):
    from aiforge_core.memory.sync import _io

    before = _io.root()
    with group.scoped("cellular"):
        assert _io.root() == before / "groups" / "cellular"
    assert _io.root() == before


def test_scoped_on_an_empty_name_is_a_no_op(tmp_path):
    """Ungrouped is a real deployment, not an error."""
    from aiforge_core.memory.sync import _io

    before = _io.root()
    with group.scoped(""):
        assert _io.root() == before


def test_scoped_refuses_an_invalid_name():
    with pytest.raises(ValueError):
        with group.scoped("../etc"):
            pass
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_group.py -v
```

Expected: FAIL — `ModuleNotFoundError: aiforge_core.memory.sync.group`.

- [ ] **Step 3: Implement**

Create `aiforge_core/memory/sync/group.py`:

```python
"""Groups: one admin, several independent fleets.

The 2026-08-18 design gave one admin one pool of knowledge — a fleet was
"every machine that names this admin". An operator running more than one pool
(different customers, different sites) wanted one hub box rather than one per
pool, and a group is the smallest thing that buys it: **a name the admin
publishes and a client selects**.

**The admin owns the list; the client learns it.** The alternative — each client
naming its own group — was rejected in design: a typo silently creates a second
pool that looks like a working sync (the client pushes happily, the admin
accepts happily) and nobody notices until somebody asks why two machines cannot
see each other's knowledge.

**No group name is hardcoded anywhere.** An admin with no groups configured runs
*ungrouped*, which is byte-for-byte the behaviour of the design this extends, so
every existing install keeps working with no configuration and no migration.

**A group is not a security boundary.** It has no key. A client states its group
and is believed, exactly as it states its peer id and is believed
(``inbox``). The check is a routing and consistency rule: it stops a
misconfigured client writing into the wrong pool, not a hostile one on the same
network. See the security posture section of the design.
"""
from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from aiforge_core.config.paths import config_dir
from aiforge_core.memory.sync import _io, paths

_log = logging.getLogger("aiforge.sync")

# States ``resolve`` can report, mirrored into ``status.state``.
OK = "ok"
NEEDS_SELECTION = "needs-group-selection"
UNKNOWN = "group-unknown"

# Where each side keeps its half. Config, not memory: both describe this
# deployment rather than knowledge, and neither may ever sync.
_LIST_FILE = "groups.json"          # the admin's published list
_CHOICE_FILE = "sync_group.json"    # the client's selection

# The directory every group's tree hangs off, below the memory root.
GROUPS_DIR = "groups"


def is_valid(name: str) -> bool:
    """True when ``name`` may be a group.

    A group name becomes a directory component under ``groups/``, so it takes
    the identity alphabet ``paths.is_addressable`` already owns — the same rule
    that guards a peer id. A name that does not round-trip is refused rather
    than repaired: repairing invents a group the operator never asked for, and
    two different bad names repair onto the same one.
    """
    return paths.is_addressable(str(name or ""))


def _list_path() -> Path:
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / _LIST_FILE


def _choice_path() -> Path:
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / _CHOICE_FILE


def known() -> list[str]:
    """The groups this admin publishes, in creation order. ``[]`` = ungrouped.

    Seeded from ``AIFORGE_SYNC_GROUPS`` only when no file exists yet, so an
    operator who has since created groups through the API is not overwritten by
    a stale line in an env file — the file is the record once it exists.
    """
    rec = _io.read_json(_list_path())
    rows = rec.get("groups")
    if isinstance(rows, list):
        return [str(g) for g in rows if is_valid(str(g))]
    seed = [g.strip() for g in (os.environ.get("AIFORGE_SYNC_GROUPS") or "").split(",")]
    return [g for g in seed if is_valid(g)]


def create(name: str) -> list[str]:
    """Publish ``name``. Idempotent. Returns the whole list."""
    if not is_valid(name):
        raise ValueError(
            f"{name!r} is not a usable group name: it becomes a directory "
            "component, so it takes the [A-Za-z0-9_-] identity alphabet")
    rows = known()
    if name not in rows:
        rows = [*rows, name]
        _io.write_json(_list_path(), {"groups": rows})
        _log.info("sync: published group %s", name)
    return rows


def selected() -> str:
    """This client's group: the pin, else the cached choice, else ""."""
    pinned = (os.environ.get("AIFORGE_SYNC_GROUP") or "").strip()
    if pinned:
        return pinned if is_valid(pinned) else ""
    return str(_io.read_json(_choice_path()).get("group") or "")


def choose(name: str) -> str:
    """Persist this client's selection. Written only on a change."""
    if not is_valid(name):
        raise ValueError(f"{name!r} is not a usable group name")
    if name != str(_io.read_json(_choice_path()).get("group") or ""):
        _io.write_json(_choice_path(), {"group": name})
        _log.info("sync: joined group %s", name)
    return name


def resolve(advertised: list[str]) -> tuple[str, str]:
    """``(group, state)`` for this cycle, given what the admin advertises.

    Order, highest first:

    1. ``AIFORGE_SYNC_GROUP`` — the operator pinned it, and discovery is not
       consulted at all. A pinned group the admin does not advertise is still
       used: the operator knows something the cycle does not, and the admin
       answers 404 if they are wrong, which is a better failure than silently
       syncing somewhere else.
    2. A cached choice. Kept even when it vanishes from the list — see below.
    3. Exactly one advertised group: select it and persist. The single-group
       deployment, which needs no UI at all.
    4. Several advertised and none chosen: ``NEEDS_SELECTION``. The caller must
       send NOTHING this cycle — half of the point is that knowledge does not
       land in the wrong pool while somebody decides.
    5. None advertised: ungrouped, the legacy behaviour.

    A cached choice that disappears from the list is **kept** and reported as
    ``UNKNOWN``, never cleared. Clearing it would re-run the auto-select in rule
    3 and move this machine's knowledge into a different pool because somebody
    was mid-edit on the admin.
    """
    pinned = (os.environ.get("AIFORGE_SYNC_GROUP") or "").strip()
    if pinned and is_valid(pinned):
        return pinned, OK

    rows = [g for g in (advertised or []) if is_valid(str(g))]
    chosen = selected()
    if chosen:
        return chosen, (OK if chosen in rows or not rows else UNKNOWN)
    if len(rows) == 1:
        return choose(rows[0]), OK
    if len(rows) > 1:
        return "", NEEDS_SELECTION
    return "", OK


@contextlib.contextmanager
def scoped(name: str):
    """Point the memory tree at ``name``'s subtree for the duration.

    This is the whole of group isolation. Every module below ``_io.root()`` —
    ``paths``, ``manifest``, ``merge``, ``apply``, ``inbox``, ``tiers`` — is
    already written against that one function, so scoping it scopes all of them
    and none of them needs to learn what a group is.

    An empty name is a no-op, because ungrouped is a real deployment rather than
    an error state.
    """
    if not name:
        yield
        return
    if not is_valid(name):
        raise ValueError(f"{name!r} is not a usable group name")
    token = _io.push_scope(_io.root() / GROUPS_DIR / name)
    try:
        yield
    finally:
        _io.pop_scope(token)


__all__ = ["OK", "NEEDS_SELECTION", "UNKNOWN", "GROUPS_DIR", "is_valid",
           "known", "create", "selected", "choose", "resolve", "scoped"]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_group.py -v
```

Expected: PASS, all 17.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/group.py tests/python/memory/sync/test_group.py
git commit -m "feat(sync): admin-owned groups, client-side selection, scoped trees"
```

---

## Task 4: The group rides every sync call

**Files:**
- Modify: `aiforge_core/api/routes/sync.py`
- Create: `aiforge_core/api/routes/groups.py`
- Modify: `aiforge_core/api/api.py:81-95`
- Modify: `aiforge_core/memory/sync/transport.py`
- Test: `tests/python/memory/sync/test_sync_routes.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/python/memory/sync/test_sync_routes.py`:

```python
def test_groups_route_lists_what_the_admin_publishes(client, monkeypatch):
    from aiforge_core.memory.sync import group

    group.create("cellular")
    group.create("retail")
    r = client.get("/api/memory/sync/groups")
    assert r.status_code == 200
    assert r.json()["groups"] == ["cellular", "retail"]
    assert r.json()["admin"]


def test_groups_route_is_empty_when_ungrouped(client):
    assert client.get("/api/memory/sync/groups").json()["groups"] == []


def test_manifest_in_an_unknown_group_is_404_and_names_the_known_ones(client):
    from aiforge_core.memory.sync import group

    group.create("cellular")
    r = client.get("/api/memory/sync/manifest", params={"group": "typo"})
    assert r.status_code == 404
    assert "cellular" in r.json()["detail"]


def test_manifest_in_a_known_group_reads_that_group_tree(client, tmp_path):
    """The route must serve the GROUP's tree, not the top-level one."""
    from aiforge_core.memory.sync import _io, group

    group.create("cellular")
    with group.scoped("cellular"):
        mesh = _io.root() / "mesh" / "nuc"
        mesh.mkdir(parents=True, exist_ok=True)
        (mesh / "M-01.md").write_text(
            "---\norigin: nuc\nkey: M-01\nrev: 1\nderived: mesh\n---\n\nx\n")

    r = client.get("/api/memory/sync/manifest", params={"group": "cellular"})
    assert [e["key"] for e in r.json()["manifest"]] == ["M-01"]
    # ...and the ungrouped tree does not see it
    assert client.get("/api/memory/sync/manifest").json()["manifest"] == []


def test_a_bad_group_name_on_a_route_is_400_not_a_new_directory(client):
    r = client.get("/api/memory/sync/manifest", params={"group": "../etc"})
    assert r.status_code == 400
```

Add to `tests/python/api/test_admin_page.py`:

```python
def test_create_group_route_publishes_it(client):
    r = client.post("/api/admin/groups", json={"name": "cellular"})
    assert r.status_code == 200
    assert r.json()["groups"] == ["cellular"]


def test_create_group_refuses_an_unusable_name(client):
    assert client.post("/api/admin/groups", json={"name": "../etc"}).status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_sync_routes.py tests/python/api/test_admin_page.py -v -k group
```

Expected: FAIL — 404 on `/api/memory/sync/groups`, and `group` is an ignored query param.

- [ ] **Step 3: Implement**

In `aiforge_core/api/routes/sync.py`, add the shared resolver and apply it to
all four routes:

```python
from typing import Annotated

from fastapi import Query


def _scope(name: str):
    """Enter the caller's group, or refuse the name.

    An unknown group is **404 naming the known ones**, never a silently created
    directory: auto-creation is exactly how a client-side typo becomes a second
    pool that nobody notices. An unusable name is 400 — it could not be a
    directory component in the first place.
    """
    from aiforge_core.memory.sync import group

    name = (name or "").strip()
    if not name:
        return group.scoped("")          # ungrouped: a no-op context
    if not group.is_valid(name):
        raise HTTPException(400, f"{name!r} is not a usable group name")
    rows = group.known()
    if name not in rows:
        raise HTTPException(
            404, f"no such group: {name}. This admin publishes: "
                 f"{', '.join(rows) or '(none — it is ungrouped)'}")
    return group.scoped(name)


GroupQ = Annotated[str, Query(description="Group to sync with; omit when ungrouped")]
```

`sync_manifest` and `sync_blob` gain the parameter and the scope:

```python
@router.get("/api/memory/sync/manifest")
def sync_manifest(group: GroupQ = "") -> dict:
    from aiforge_core.memory.sync import identity, inbox, role

    with _scope(group):
        return {"manifest": inbox.downstream(), "admin": identity.self_id(),
                "role": role.role(), "group": group}
```

```python
@router.get("/api/memory/sync/blob/{digest}", responses={404: {"description": "Not found"}})
def sync_blob(digest: str, group: GroupQ = "") -> Response:
    from aiforge_core.memory.sync import inbox
    from aiforge_core.memory.sync import manifest as _man

    digest = (digest or "").strip().lower()
    with _scope(group):
        if digest not in {str(e.get("hash") or "") for e in inbox.downstream()}:
            raise HTTPException(404, f"no blob: {digest}")
        path = _man.path_for_hash(digest)
        if path is None:
            raise HTTPException(404, f"no blob: {digest}")
        return Response(content=path.read_bytes(), media_type="text/markdown")
```

`sync_offer` and `sync_push` read it from the body — the group travels with the
peer id it belongs to:

```python
    entries = payload.get("entries")
    with _scope(str(payload.get("group") or "")):
        inbox.seen(str(payload.get("peer") or ""))
        return {"want": inbox.wanted(entries if isinstance(entries, list) else [])}
```

```python
    entry = payload.get("entry")
    with _scope(str(payload.get("group") or "")):
        return {"applied": inbox.accept(str(payload.get("peer") or ""),
                                        entry if isinstance(entry, dict) else {}, body)}
```

Add the groups route to `sync.py`:

```python
@router.get("/api/memory/sync/groups")
def sync_groups() -> dict:
    """What a client may join. Open, like the rest of this surface.

    Discovery is what stops an operator configuring the same fact on every
    machine: a client already knows the admin's url, so the list is one hop
    away and the client picks from it rather than restating it.
    """
    from aiforge_core.memory.sync import group, identity

    return {"groups": group.known(), "admin": identity.self_id()}
```

Create `aiforge_core/api/routes/groups.py` for the operator half (loopback-only,
reusing the admin dependency):

```python
"""Operator routes for groups and revert (/api/admin/groups…).

Loopback-only, on the same ``_require_loopback`` dependency the rest of
``/admin`` uses — these routes CHANGE what the fleet syncs and where it can be
rolled back to, so they are firmly on the control plane rather than the open
sync surface.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from aiforge_core.api.routes.admin import _require_loopback

router = APIRouter(dependencies=[Depends(_require_loopback)])


@router.get("/api/admin/groups")
def list_groups() -> dict:
    from aiforge_core.memory.sync import group

    return {"groups": group.known()}


@router.post("/api/admin/groups", responses={400: {"description": "Bad name"}})
async def create_group(request: Request) -> dict:
    from aiforge_core.memory.sync import group

    payload = await request.json()
    name = str((payload or {}).get("name") or "").strip()
    try:
        return {"groups": group.create(name)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
```

Register it in `aiforge_core/api/api.py` beside the others:

```python
from aiforge_core.api.routes import groups as _r_groups  # noqa: E402
...
app.include_router(_r_groups.router)
```

In `transport.py`, thread the group through every call. Add a helper and use it:

```python
def _q(group: str) -> str:
    """The group as a query string fragment, or "" when ungrouped.

    Quoted, because the name reaches here from configuration and a value that
    needs escaping must not silently address a different route.
    """
    from urllib.parse import quote

    return f"?group={quote(group, safe='')}" if group else ""
```

`fetch_manifest(base_url, token="", group="")` →
`f"{base}/api/memory/sync/manifest{_q(group)}"`.
`fetch_blob(base_url, digest, token="", group="")` →
`f"{base}/api/memory/sync/blob/{digest}{_q(group)}"`.
`offer(base_url, entries, group="")` and `push_blob(base_url, entry, body,
group="")` add `"group": group` to their JSON envelopes.

Add:

```python
def fetch_groups(base_url: str) -> list[str] | None:
    """What the admin publishes. ``None`` when it could not be reached.

    None and ``[]`` are different answers and the caller must tell them apart:
    ``[]`` is a reachable ungrouped admin (sync normally), None is an admin that
    is down (do nothing this cycle).
    """
    import json

    try:
        raw = _fetch(f"{base_url.rstrip('/')}/api/memory/sync/groups", _token(),
                     MAX_MANIFEST_BYTES)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — an unreachable admin is expected
        _log.info("sync: admin %s did not answer the group list: %s", base_url, exc)
        return None
    rows = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    return [str(g) for g in rows][:MAX_MANIFEST_ENTRIES]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_sync_routes.py tests/python/api/test_admin_page.py tests/python/api/test_sync_endpoint_auth.py -v
```

Expected: PASS, including every pre-existing route test — the group parameter
defaults to `""`, so an ungrouped caller is unchanged.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/api/routes/sync.py aiforge_core/api/routes/groups.py \
        aiforge_core/api/api.py aiforge_core/memory/sync/transport.py \
        tests/python/memory/sync/test_sync_routes.py tests/python/api/test_admin_page.py
git commit -m "feat(sync): the group rides every sync call, and the admin publishes its list"
```

---

## Task 5: The client resolves its group before it sends anything

**Files:**
- Modify: `aiforge_core/memory/sync/loop.py` (`run_once`, `sync_with`, `_pull`)
- Modify: `aiforge_core/memory/sync/push.py` (`run_once`)
- Test: `tests/python/memory/sync/test_group_isolation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_group_isolation.py`:

```python
"""Two groups on one admin never see each other's knowledge.

This is the test that catches a leaked scope: if ``_io``'s ContextVar override
were an env var, or if a route forgot to enter the scope, one group's node would
show up in the other's manifest here.
"""
from __future__ import annotations

import pytest

from tests.python.memory.sync import _hub


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    admin = _hub.node(monkeypatch, tmp_path, "nuc")
    with _hub.serving(admin):
        from aiforge_core.memory.sync import group
        group.create("cellular")
        group.create("retail")
    a = _hub.node(monkeypatch, tmp_path, "ms", admin_url="http://admin")
    b = _hub.node(monkeypatch, tmp_path, "lap", admin_url="http://admin")
    return admin, a, b


def test_several_groups_halt_a_client_that_has_not_chosen(fleet, monkeypatch):
    admin, a, _ = fleet
    _hub.activate(monkeypatch, a)
    _hub.author(a, "O-01", "a note about the invoice parser")

    rows = _hub.run_cycle(monkeypatch, a, admin)

    assert rows[0]["state"] == "needs-group-selection"
    assert rows[0]["pushed"] == 0
    with _hub.serving(admin):
        from aiforge_core.memory.sync import group, manifest
        for g in group.known():
            with group.scoped(g):
                assert manifest.build() == []


def test_a_chosen_group_receives_the_push_and_the_other_does_not(fleet, monkeypatch):
    admin, a, _ = fleet
    _hub.activate(monkeypatch, a)
    from aiforge_core.memory.sync import group as _g
    _g.choose("cellular")
    _hub.author(a, "O-01", "a note about the invoice parser")

    _hub.run_cycle(monkeypatch, a, admin)

    with _hub.serving(admin):
        from aiforge_core.memory.sync import group, manifest
        with group.scoped("cellular"):
            assert [e["key"] for e in manifest.build()] == ["O-01"]
        with group.scoped("retail"):
            assert manifest.build() == []


def test_one_group_cannot_read_the_other_group_blob(fleet, monkeypatch):
    admin, a, b = fleet
    _hub.activate(monkeypatch, a)
    from aiforge_core.memory.sync import group as _g
    _g.choose("cellular")
    _hub.author(a, "O-01", "a note about the invoice parser")
    _hub.run_cycle(monkeypatch, a, admin)

    with _hub.serving(admin):
        from aiforge_core.memory.sync import group, manifest
        with group.scoped("cellular"):
            digest = manifest.build()[0]["hash"]

    r = admin["client"].get(f"/api/memory/sync/blob/{digest}",
                            params={"group": "retail"})
    assert r.status_code == 404


def test_a_single_group_admin_needs_no_client_configuration(tmp_path, monkeypatch):
    admin = _hub.node(monkeypatch, tmp_path, "nuc2")
    with _hub.serving(admin):
        from aiforge_core.memory.sync import group
        group.create("cellular")
    spoke = _hub.node(monkeypatch, tmp_path, "ms2", admin_url="http://admin")
    _hub.activate(monkeypatch, spoke)
    _hub.author(spoke, "O-01", "a note about the invoice parser")

    rows = _hub.run_cycle(monkeypatch, spoke, admin)

    assert rows[0]["group"] == "cellular"
    assert rows[0]["pushed"] == 1
```

Add the two helpers `_hub.author` and `_hub.run_cycle` to
`tests/python/memory/sync/_hub.py`:

```python
def author(machine, key: str, body: str, *, rev: int = 1) -> None:
    """Write one authored node into this machine's own ``okf/``.

    Bodies carry a file path and an identifier on purpose: the outbound filter
    (``sync.redact``) holds back a note with no project signal at all, so a
    fixture that says only "hello" would be filtered and every convergence
    assertion would fail for the wrong reason.
    """
    from aiforge_core.memory.sync import _io

    okf = _io.root() / "okf"
    okf.mkdir(parents=True, exist_ok=True)
    (okf / f"{key}.md").write_text(
        f"---\norigin: {machine['name']}\nkey: {key}\nrev: {rev}\n---\n\n"
        f"{body}\n\nSee `aiforge_core/memory/sync/loop.py` — `run_once()`.\n",
        encoding="utf-8")


def run_cycle(monkeypatch, spoke, admin) -> list[dict]:
    """One real cycle: the spoke's push + pull against the admin's real routes."""
    import httpx

    _hub_client = admin["client"]

    def _request(method, url, **kw):
        path = url.split("http://admin", 1)[-1]
        with serving(admin):
            return _hub_client.request(method, path, **kw)

    monkeypatch.setattr(httpx.Client, "request", _request, raising=False)
    activate(monkeypatch, spoke)
    from aiforge_core.memory.sync import loop
    return loop.run_once()
```

If `_hub.py` already routes transport at the admin's `TestClient` by another
mechanism, reuse that one instead of adding a second — read the file first.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_group_isolation.py -v
```

Expected: FAIL — `KeyError: 'state'`; the cycle knows nothing about groups.

- [ ] **Step 3: Implement**

In `loop.py`, `run_once` resolves the group first and refuses to send when the
answer is ambiguous:

```python
def run_once() -> list[dict]:
    """One cycle. Never raises.

    The GROUP is resolved before anything is sent. When the admin publishes
    several and this machine has chosen none, the cycle stops here: knowledge
    landing in the wrong pool is not recoverable by choosing correctly later,
    because the wrong pool has already folded it and served it onward.
    """
    from aiforge_core.memory.sync import group, role, status, transport

    if role.is_admin():
        return []
    base = role.admin_url()
    if not base:
        status.record(state="no-admin", admin="", reachable=False)
        return []

    advertised = transport.fetch_groups(base)
    if advertised is None:
        status.record(state="unreachable", admin=base, reachable=False,
                      group=group.selected())
        return [{"admin": base, "ok": False, "state": "unreachable",
                 "group": group.selected(), "pushed": 0, "applied": 0,
                 "rejected": 0, "conflicts": 0}]

    chosen, state = group.resolve(advertised)
    if state == group.NEEDS_SELECTION:
        status.record(state=state, admin=base, reachable=True, group="",
                      groups_available=advertised)
        return [{"admin": base, "ok": True, "state": state, "group": "",
                 "pushed": 0, "applied": 0, "rejected": 0, "conflicts": 0}]

    deadline = time.monotonic() + CYCLE_BUDGET
    row = {"admin": base, "group": chosen, "state": state}
    try:
        row.update(sync_with(base, deadline, group=chosen))
    except Exception as exc:  # noqa: BLE001
        _log.warning("sync: cycle failed for %s: %s", base, exc)
    status.record(state=state if row.get("ok") else "unreachable", admin=base,
                  reachable=bool(row.get("ok")), group=chosen,
                  groups_available=advertised, pending=row.get("pending", 0),
                  pushed=row.get("pushed", 0))
    return [row]
```

`sync_with(base_url, deadline=None, *, group="")` passes it to
`push.run_once(base_url, deadline, group=group)` and to
`_pull(base_url, result, deadline, group)`. `_pull` passes it to
`transport.fetch_manifest(base_url, group=group)` and
`transport.fetch_blob(base_url, hash, group=group)`; `_preserve_conflicts`
likewise. `push.run_once` passes it to `transport.offer(base_url, entries,
group=group)` and `transport.push_blob(..., group=group)`, and records
`result["pending"] = len(want)` after the offer so the status has it.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/ -v
```

Expected: PASS, including `test_hub_cycle.py` and `test_cycle_budget.py`
unchanged — an ungrouped admin advertises `[]`, resolve returns `("", "ok")`,
and every call passes an empty group.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/loop.py aiforge_core/memory/sync/push.py \
        tests/python/memory/sync/_hub.py tests/python/memory/sync/test_group_isolation.py
git commit -m "feat(sync): resolve the group before sending, and halt rather than guess"
```

---

## Task 6: The admin folds once per group

**Files:**
- Modify: `aiforge_core/memory/okf/tiers.py` (`distil_mesh`, `run_after_sync`)
- Test: `tests/python/memory/test_okf_tiers.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/python/memory/test_okf_tiers.py`:

```python
def test_the_admin_folds_each_group_separately(tmp_path, monkeypatch):
    """One fold per group, each reading only that group's inputs."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import _io, group

    group.create("cellular")
    group.create("retail")
    for g, key in (("cellular", "O-01"), ("retail", "O-02")):
        with group.scoped(g):
            d = _io.root() / "peers" / "ms"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{key}.md").write_text(
                f"---\norigin: ms\nkey: {key}\nrev: 1\n---\n\n"
                f"note for {g} in `x/y.py`\n", encoding="utf-8")

    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        tiers, "_run_tier",
        lambda **kw: seen.append((str(kw["directory"]),
                                  sorted(n["meta"]["key"] for n in kw["inputs"])))
        or {"ok": True})

    tiers.distil_mesh()

    assert len(seen) == 2
    assert any("groups/cellular" in d and keys == ["O-01"] for d, keys in seen)
    assert any("groups/retail" in d and keys == ["O-02"] for d, keys in seen)


def test_an_ungrouped_admin_folds_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg2"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md2"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import _io

    d = _io.root() / "peers" / "ms"
    d.mkdir(parents=True, exist_ok=True)
    (d / "O-01.md").write_text(
        "---\norigin: ms\nkey: O-01\nrev: 1\n---\n\nnote in `x/y.py`\n",
        encoding="utf-8")

    calls = []
    monkeypatch.setattr(tiers, "_run_tier",
                        lambda **kw: calls.append(str(kw["directory"])) or {"ok": True})
    tiers.distil_mesh()
    assert len(calls) == 1
    assert "groups/" not in calls[0]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/python/memory/test_okf_tiers.py -v -k group
```

Expected: FAIL — `assert 1 == 2`; the fold runs once against the top-level tree.

- [ ] **Step 3: Implement**

Rename the current body of `distil_mesh` to `_distil_one` (it is already the
whole of the per-tree fold and needs no change beyond the name), then:

```python
def distil_mesh(*, role: str = _ROLE) -> dict:
    """Fold authored knowledge into ``mesh/`` — once per group.

    Admin-only, unchanged (``role.may_merge`` owns that policy and its
    soft-fail direction). What is new is that a hub may serve several groups,
    and each is a separate tree with separate inputs: a fold that read them
    together would put one fleet's knowledge in another fleet's view.

    An admin with no groups folds its one tree exactly as before — this is the
    ungrouped deployment, and the loop below degenerates to a single pass.
    """
    from aiforge_core.memory.sync import group, role as _role

    if not _role.may_merge():
        return {"ok": True, "skipped": "not-admin", "admin": _role.admin_id()}

    groups = group.known()
    if not groups:
        return _distil_one(role=role)

    out: dict = {"ok": True, "groups": {}}
    for name in groups:
        try:
            with group.scoped(name):
                out["groups"][name] = _distil_one(role=role)
        except Exception as exc:  # noqa: BLE001 — one bad group is not the rest
            # A fold that dies takes its own group's cycle, never the hub: the
            # other groups' knowledge is unrelated and must keep converging.
            _log.warning("okf: mesh fold failed for group %s: %s", name, exc)
            out["groups"][name] = {"ok": False, "error": str(exc)[:200]}
    return out
```

`_distil_one` must not re-check `may_merge` (the caller did) — delete that
branch from it and keep everything below.

`run_after_sync` needs the same treatment for `build_view`: it runs on every
machine, and on the admin it must build one view per group. Wrap its
`build_view` call the same way, iterating `group.known()` and falling through to
a single unscoped call when the list is empty.

Remove the `xfail` marker from `test_a_full_cycle_leaves_okf_untouched` in
`tests/python/memory/sync/test_untouchable_okf.py` — `_hub.run_cycle` exists as
of Task 5.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/python/memory/ -v
```

Expected: PASS, including `test_okf_tiers_hardening.py` and
`test_retire_demoted_mesh.py`.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/okf/tiers.py tests/python/memory/test_okf_tiers.py \
        tests/python/memory/sync/test_untouchable_okf.py
git commit -m "feat(okf): the admin folds each group separately"
```

---

## Task 7: The filter — credentials

**Files:**
- Create: `aiforge_core/memory/sync/redact/__init__.py`
- Create: `aiforge_core/memory/sync/redact/_text.py`
- Create: `aiforge_core/memory/sync/redact/secrets.py`
- Test: `tests/python/memory/sync/test_redact.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/python/memory/sync/test_redact.py`:

```python
"""The outbound filter, rule by rule, in both directions.

Both directions matter more than coverage does: a rule that blocks everything
passes a one-sided test and silently stops the fleet syncing at all.
"""
from __future__ import annotations

import pytest

from aiforge_core.memory.sync import redact


def node(body: str, title: str = "Invoice parser rounding", **meta) -> dict:
    return {"meta": {"key": "O-01", "origin": "ms", "title": title, **meta},
            "body": body}


# ── secrets: blocked ─────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "deploy key AKIAIOSFODNN7EXAMPLE is in the CI vars",
    "token ghp_16C7e42F292c6912E7710c838347Ae178B4a",
    "github_pat_11ABCDEFG0aBcDeFgHiJkL_mNoPqRsTuVwXyZ0123456789abcdefghij",
    "slack hook xoxb-2401-4567-abcdefghijklmnopqrstuvwx",
    "maps key AIzaSyD-1234567890abcdefghijklmnopqrstu",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g",
    "run with password=hunter2ThatIsReal",
    "export API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD",
    "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789'",
    "psql postgres://admin:s3cr3tp4ss@db.internal:5432/oneshell",
])
def test_a_credential_blocks_the_whole_node(body):
    v = redact.review(node(body))
    assert v.send is False
    assert v.rule.startswith("secrets.")


def test_the_reason_never_quotes_the_secret():
    """The block log is written to disk; it must not become the leak."""
    v = redact.review(node("password=hunter2ThatIsReal"))
    assert "hunter2ThatIsReal" not in v.reason


# ── secrets: allowed ─────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "the AKIA prefix identifies an AWS access key id",
    "set `password=` from the vault, never inline — see `deploy/env.py`",
    "PASSWORD is read from the environment in `aiforge_core/config/env.py`",
    "commit 9f8e7d6c5b4a39281706f5e4d3c2b1a098765432 fixed `loop.py`",
    "id 3f2504e0-4f89-11d3-9a0c-0305e82c3301 in `Parties` collection",
    "base64 body `aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBzZWNyZXQ=` in `web_ingest.py`",
])
def test_prose_about_credentials_is_not_a_credential(body):
    assert redact.review(node(body)).send is True
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_redact.py -v
```

Expected: FAIL — `ModuleNotFoundError: aiforge_core.memory.sync.redact`.

- [ ] **Step 3: Implement**

Create `aiforge_core/memory/sync/redact/_text.py`:

```python
"""Turning a node into the text the rules read, and measuring its substance.

One place, so the three rule modules cannot disagree about what "the node" is —
a rule reading only the body while another reads the title is how a secret in a
title travels.
"""
from __future__ import annotations

import re

# Markdown furniture that says nothing about whether a node is knowledge.
_FURNITURE = re.compile(r"^\s*(?:[-*+]\s+|#+\s+|>\s+|\d+\.\s+)", re.MULTILINE)
_FENCE = re.compile(r"```.*?```", re.DOTALL)


def text_of(node: dict) -> str:
    """Everything a rule may read: the title and the body, one string."""
    meta = node.get("meta") or {}
    title = str(meta.get("title") or meta.get("key") or "")
    return f"{title}\n{node.get('body') or ''}"


def substance(node: dict) -> str:
    """The body with markdown furniture removed — what is left to judge."""
    body = str(node.get("body") or "")
    return _FURNITURE.sub("", body).strip()


def code_fences(node: dict) -> list[str]:
    return _FENCE.findall(str(node.get("body") or ""))
```

Create `aiforge_core/memory/sync/redact/secrets.py`:

```python
"""Credential detection: the half of the filter that must not be wrong.

**A match blocks the whole node**, it does not scrub the matched span. A note
that mentions a credential is a note *about* that credential — its title, the
sentence around it and the file path it names usually identify the system too.
Scrubbing ships all of that, and ships it carrying an implicit claim of safety.
Blocking is also auditable: the operator sees "held back, rule
``secrets.aws_key``" and can go and look.

Two halves, on purpose:

* **Known shapes** — precise, essentially no false positives, and they cover
  the credentials that actually leak (a cloud key pasted from a console, a
  token from a CI page). This is the reliable half.
* **An entropy heuristic** — a `KEY = <long random-looking value>` where the
  key name reads secret-ish. This is the recall half, and the one to tune from
  the block log, because it is the one that can be wrong.

The reason string a rule returns names the RULE and never quotes the match: the
block log is written to disk, and a log that records the secret is the leak.
"""
from __future__ import annotations

import math
import re

from aiforge_core.memory.sync.redact import _text

# Known credential shapes. Anchored on the vendor prefix plus a length, because
# the prefix alone appears in prose that legitimately explains it ("the AKIA
# prefix identifies an AWS access key id" must sync).
_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("slack_token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("llm_key", re.compile(r"\b(?:sk|sk-ant|sk-proj)-[A-Za-z0-9_-]{20,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}"
                       r"\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b")),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
)

# `key = value` where the key reads secret-ish. The VALUE is what decides:
# an empty value, or one that is plainly a placeholder or a reference, is the
# correct way to write about a secret and must keep syncing.
_ASSIGN = re.compile(
    r"\b(?P<key>[A-Za-z_][A-Za-z0-9_]*"
    r"(?:passwo?rd|passwd|secret|api[_-]?key|apikey|token|credential)s?)"
    r"\s*[:=]\s*(?P<q>[\"']?)(?P<val>[^\s\"']{8,})(?P=q)",
    re.IGNORECASE)

# A value shorter than this is not worth blocking a node over — real
# credentials are longer, and short values are overwhelmingly placeholders.
_MIN_SECRET_LEN = 8

# Shannon bits per character above which a value looks generated rather than
# written. English prose sits near 2.5-3.0; base64 key material sits above 4.0.
# 3.6 keeps "hunter2ThatIsReal" (mixed case, 3.7) blocked while leaving ordinary
# words and dotted paths alone. Tune from the block log, not from intuition.
_MIN_ENTROPY = 3.6

# Values that are how one CORRECTLY writes about a secret.
_PLACEHOLDERS = re.compile(
    r"^(?:\.{3}|x+|<[^>]*>|\{\{?[^}]*\}?\}|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|"
    r"your[_-]?\w+|changeme|redacted|placeholder|example|dummy|none|null|"
    r"true|false|\d+)$",
    re.IGNORECASE)


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _assignment_hit(text: str) -> str:
    """The rule name for a secret-looking assignment, or "".

    A value is a secret when it is long enough, is not a placeholder, is not a
    reference to somewhere the value actually lives, and does not read like
    prose. All four are required: dropping any one of them blocks a note that
    documents configuration, which is exactly the knowledge worth syncing.
    """
    for m in _ASSIGN.finditer(text):
        val = m.group("val")
        if len(val) < _MIN_SECRET_LEN or _PLACEHOLDERS.match(val):
            continue
        if val.startswith(("$", "{", "<", "`")):
            continue          # a reference, not a value
        if _entropy(val) < _MIN_ENTROPY:
            continue
        return f"secrets.{m.group('key').lower()}"
    return ""


def check(node: dict) -> tuple[str, str]:
    """``(rule, reason)`` when this node carries a credential, else ``("", "")``.

    The reason names the rule and NEVER the match — see the module docstring.
    """
    text = _text.text_of(node)
    for name, pattern in _SHAPES:
        if pattern.search(text):
            return (f"secrets.{name}",
                    f"the note contains something shaped like a {name.replace('_', ' ')}")
    rule = _assignment_hit(text)
    if rule:
        return rule, "the note assigns a high-entropy value to a credential-shaped name"
    return "", ""


__all__ = ["check"]
```

Create `aiforge_core/memory/sync/redact/__init__.py`:

```python
"""The outbound filter: what this machine will let leave it.

Runs on the CLIENT, in the push path, before anything is advertised — so a
blocked node never appears in an offer and the admin never learns it exists.
Re-run on the admin in ``inbox.accept`` as defence in depth, so a client build
that predates this package cannot leak into a group.

A package rather than a process. A filter that can be *down* is a filter that
stops sync, and this one has to be able to fail without taking the daemon with
it; it also has to be cheap enough to run on every node of every cycle. Every
rule is deterministic — no LLM in the sync path, so the filter costs nothing,
never rate-limits and cannot wedge a cycle when a model is unreachable.

Three stages, first refusal wins:

* ``secrets``  — credentials. The half that must not be wrong.
* ``private``  — somebody's own machine, not the fleet's knowledge.
* ``noise``    — the idle-search class: the user asked what the capital of
                 France is, it became a capture, the capture became a node.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from aiforge_core.memory.sync.redact import noise, private, secrets

_log = logging.getLogger("aiforge.sync")


@dataclass(frozen=True)
class Verdict:
    """Why a node may or may not leave. ``rule``/``reason`` are "" when it may."""
    send: bool
    rule: str = ""
    reason: str = ""


_STAGES = (("secrets", secrets.check), ("private", private.check),
           ("noise", noise.check))


def review(node: dict) -> Verdict:
    """Whether ``node`` may leave this machine.

    **Fails CLOSED.** A rule that raises means we do not know whether the node
    is safe, and the whole point of the stage is that we do not send what we
    cannot vouch for. The node is simply re-offered next cycle, so a bug here
    costs a delay rather than a leak.
    """
    for name, check in _STAGES:
        try:
            rule, reason = check(node)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            _log.warning("sync: filter stage %s failed, holding the node back: %s",
                         name, exc)
            return Verdict(False, f"{name}.error", "the filter could not judge this note")
        if rule:
            return Verdict(False, rule, reason)
    return Verdict(True)


def explain() -> list[dict]:
    """The rules, for the settings screen. Ordered as they are applied."""
    return [{"stage": name, "rules": check.__module__.split(".")[-1],
             "doc": (check.__doc__ or "").strip().splitlines()[0]}
            for name, check in _STAGES]


__all__ = ["Verdict", "review", "explain"]
```

`private.py` and `noise.py` are Task 8; until then create them with a stub
`def check(node): return "", ""` and a one-line docstring so the import works.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_redact.py -v
```

Expected: PASS — 12 blocked cases, 6 allowed, 1 reason check.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/redact/ tests/python/memory/sync/test_redact.py
git commit -m "feat(sync): outbound credential filter"
```

---

## Task 8: The filter — private scope and idle-search noise

**Files:**
- Modify: `aiforge_core/memory/sync/redact/private.py`
- Modify: `aiforge_core/memory/sync/redact/noise.py`
- Test: `tests/python/memory/sync/test_redact.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/python/memory/sync/test_redact.py`:

```python
# ── noise: blocked ───────────────────────────────────────────────────────

@pytest.mark.parametrize("title,body", [
    ("What is the capital of France", "Paris."),
    ("Brazil", "A country in South America. Capital Brasilia."),
    ("How do I center a div", ""),
    ("Weather", "It is 24 degrees today."),
    ("Note", "ok"),
])
def test_an_idle_search_does_not_leave_the_machine(title, body):
    v = redact.review(node(body, title=title))
    assert v.send is False
    assert v.rule.startswith("noise.")


# ── noise: allowed ───────────────────────────────────────────────────────

@pytest.mark.parametrize("title,body", [
    ("Invoice parser rounding",
     "`PosClientBackend` rounds item discount before tax; see "
     "`aiforge_core/memory/sync/loop.py`."),
    ("NATS retry backoff",
     "The consumer retries at 5s, 30s, 120s then terms to the DLQ."),
    ("MongoDbService is mandatory",
     "Never query MongoDB directly — go through `MongoDbService`."),
    ("Sonar S3776",
     "Cognitive complexity must be <= 15; `run_chat_agent` was decomposed."),
])
def test_real_project_knowledge_syncs(title, body):
    assert redact.review(node(body, title=title)).send is True


# ── private ──────────────────────────────────────────────────────────────

def test_a_locally_scoped_note_stays_local():
    v = redact.review(node("the parser lives in `x/y.py`", scope="local"))
    assert v.send is False
    assert v.rule == "private.scope"


@pytest.mark.parametrize("scope", ["global", "project", "", None])
def test_every_other_scope_syncs(scope):
    assert redact.review(node("the parser lives in `x/y.py`", scope=scope)).send is True


def test_a_note_only_about_a_home_path_stays_local():
    v = redact.review(node("my dotfiles are in /Users/manip/.zshrc"))
    assert v.send is False
    assert v.rule == "private.home_path"


def test_a_home_path_alongside_project_knowledge_still_syncs():
    body = ("the venv is at /Users/manip/.venv but the fix is in "
            "`aiforge_core/memory/sync/loop.py` — `run_once()`")
    assert redact.review(node(body)).send is True


# ── thresholds are configurable ──────────────────────────────────────────

def test_the_substance_threshold_is_tunable(monkeypatch):
    monkeypatch.setenv("AIFORGE_FILTER_MIN_SUBSTANCE", "10000")
    from aiforge_core.memory.sync.redact import noise
    v = noise.check(node("`x/y.py` has a real fix in `run_once()`"))
    assert v[0] == "noise.thin"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_redact.py -v -k "noise or private or substance"
```

Expected: FAIL — the stubs return `("", "")`, so everything sends.

- [ ] **Step 3: Implement**

Replace `aiforge_core/memory/sync/redact/private.py`:

```python
"""Somebody's own machine is not the fleet's knowledge.

Two rules, both narrow on purpose. This stage exists to stop the obvious
personal note travelling, not to be a privacy classifier — a broad rule here
silently starves the fleet, which is a worse failure than an over-shared note
about a dotfile.
"""
from __future__ import annotations

import os
import re

from aiforge_core.memory.sync.redact import _text

# The scope value md_store already writes for knowledge that is about this
# machine and this user. Anything else — global, project, unset — syncs.
_LOCAL_SCOPES = {"local", "personal", "private"}

# A path under a home directory. Matched generically rather than against
# ``Path.home()``: a note may name another machine's home, and the rule is about
# the SHAPE of the reference, not about whose home it is.
_HOME_PATH = re.compile(r"(?:/Users/|/home/|~/)[\w.@-]+", re.IGNORECASE)

# Signals that a note is about the codebase rather than about a machine.
_PROJECT = re.compile(
    r"`[^`\n]+`"                                   # any inline code span
    r"|\b[\w-]+\.(?:py|ts|tsx|java|go|rs|sql|ya?ml|json|sh)\b"   # a filename
    r"|\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b"            # a CamelCase identifier
    r"|\b\w+\(\)"                                  # a call
)


def check(node: dict) -> tuple[str, str]:
    """Personal notes: a local scope, or a note whose only referent is a home path."""
    scope = str((node.get("meta") or {}).get("scope") or "").strip().lower()
    if scope in _LOCAL_SCOPES:
        return "private.scope", f"the note is scoped {scope}, so it stays on this machine"

    text = _text.text_of(node)
    if _HOME_PATH.search(text) and not _PROJECT.search(text):
        # A home path ALONGSIDE project signal is ordinary knowledge ("the venv
        # is at ~/.venv but the fix is in loop.py") and must keep syncing; a
        # home path that is the whole note is somebody's own setup.
        return ("private.home_path",
                "the note is only about a path in a home directory")
    return "", ""


__all__ = ["check"]
```

Replace `aiforge_core/memory/sync/redact/noise.py`:

```python
"""The idle-search class: what the user asked, not what the fleet learned.

A user asks the assistant something idle — a country, a definition, how to
center a div. It becomes a capture, the capture becomes a node, and the node
syncs to every machine in the fleet forever. None of it is knowledge about the
work, and at volume it crowds out the knowledge that is.

Every threshold below is a named constant with an env override, because the
right value is discovered from the block log rather than argued about up front.
Each rule is reported by name for the same reason: an operator seeing
"noise.no_project_signal held back 40 notes" knows which dial to turn.
"""
from __future__ import annotations

import os
import re

from aiforge_core.memory.sync.redact import _text


def _threshold(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except ValueError:
        return default


# Characters of real content, once markdown furniture is stripped. Below this a
# node cannot carry a fact worth replicating — it is a stub or a one-word answer.
def _min_substance() -> int:
    return _threshold("AIFORGE_FILTER_MIN_SUBSTANCE", 80)


# Words in the whole node below which "is this about the work" cannot be judged
# from content at all, so the project-signal rule is the only one that applies.
def _min_words() -> int:
    return _threshold("AIFORGE_FILTER_MIN_WORDS", 8)


# Anything that says a note is about this codebase: an inline code span, a
# filename with a code extension, a path, a CamelCase identifier, a call, a
# command, an error, an id. Deliberately generous — the cost of a false
# "this is project knowledge" is one extra synced note; the cost of a false
# negative is a real fact never leaving the machine that learned it.
_PROJECT_SIGNAL = re.compile(
    r"`[^`\n]+`"
    r"|\b[\w-]+\.(?:py|ts|tsx|java|go|rs|sql|ya?ml|json|sh|md)\b"
    r"|(?:^|\s)/[\w./-]+"
    r"|\b[a-z]+(?:[A-Z][a-z]+)+\b"
    r"|\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b"
    r"|\b\w+\(\)"
    r"|\b(?:error|exception|traceback|timeout|failed|regression|commit|"
    r"branch|deploy|endpoint|schema|migration|threshold)\b"
    r"|\b[0-9a-f]{7,40}\b",
    re.IGNORECASE)

# A title that asks rather than states. Not blocked on its own — a question is
# a fine title when the body answers it — only when the body resolves nothing.
_QUESTION = re.compile(
    r"^\s*(?:what|who|where|when|why|how|which|is|are|does|do|can|should)\b"
    r"|\?\s*$", re.IGNORECASE)

# A body that is one dictionary fact about one proper noun: "A country in South
# America. Capital Brasilia." Matched on the shape, since the entity itself is
# whatever the user happened to ask about.
_ENCYCLOPAEDIC = re.compile(
    r"\b(?:is a|is an|is the|are the|refers to|stands for|capital|population|"
    r"located in|founded in|born in|invented by)\b", re.IGNORECASE)


def check(node: dict) -> tuple[str, str]:
    """The first noise rule this node trips, or ``("", "")``.

    Order matters: the cheap structural rules run before the content ones, so
    the common case (a real note, long and full of code spans) is one regex.
    """
    text = _text.text_of(node)
    body = _text.substance(node)
    title = str((node.get("meta") or {}).get("title") or "")
    signal = bool(_PROJECT_SIGNAL.search(text))

    if len(body) < _min_substance() and not signal:
        return ("noise.thin",
                f"under {_min_substance()} characters of content and nothing "
                "identifying the work")
    if not signal and len(text.split()) < _min_words():
        return "noise.no_project_signal", "nothing in the note identifies the work"
    if not signal and _ENCYCLOPAEDIC.search(body):
        return ("noise.encyclopaedic",
                "the note reads as a general fact rather than something learned here")
    if not signal and _QUESTION.search(title) and len(body) < _min_substance():
        return "noise.unanswered", "a question whose body answers nothing"
    if not signal:
        return "noise.no_project_signal", "nothing in the note identifies the work"
    if len(body) < _min_substance():
        return ("noise.thin",
                f"under {_min_substance()} characters of content")
    return "", ""


__all__ = ["check"]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_redact.py -v
```

Expected: PASS, all of them. If a "real project knowledge" case blocks, the
threshold is wrong, not the test — the allowed set is the contract.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/redact/ tests/python/memory/sync/test_redact.py
git commit -m "feat(sync): filter personal notes and idle-search noise out of the push"
```

---

## Task 9: Wire the filter into the push, and log what it held back

**Files:**
- Modify: `aiforge_core/memory/sync/push.py` (`_mine`)
- Modify: `aiforge_core/memory/sync/inbox.py` (`accept`)
- Create: the block log inside `aiforge_core/memory/sync/status.py` (Task 10 owns the file; add the ring here)
- Test: `tests/python/memory/sync/test_redact.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/python/memory/sync/test_redact.py`:

```python
def test_a_blocked_node_is_never_advertised(tmp_path, monkeypatch):
    """The meaningful line of defence: the bytes do not leave the machine."""
    from tests.python.memory.sync import _hub

    admin = _hub.node(monkeypatch, tmp_path, "nuc")
    spoke = _hub.node(monkeypatch, tmp_path, "ms", admin_url="http://admin")
    _hub.activate(monkeypatch, spoke)
    _hub.author(spoke, "O-01", "the parser is in `x/y.py` and `run_once()` fixed it")

    from aiforge_core.memory.sync import _io
    (_io.root() / "okf" / "O-02.md").write_text(
        "---\norigin: ms\nkey: O-02\nrev: 1\n---\n\n"
        "deploy key AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")

    rows = _hub.run_cycle(monkeypatch, spoke, admin)

    assert rows[0]["pushed"] == 1
    assert rows[0]["blocked"] == 1
    with _hub.serving(admin):
        from aiforge_core.memory.sync import manifest
        assert [e["key"] for e in manifest.build()] == ["O-01"]


def test_the_admin_re_runs_the_filter_on_what_is_pushed(tmp_path, monkeypatch):
    """Defence in depth: an old client build cannot leak into a group."""
    import hashlib

    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    from aiforge_core.memory.sync import inbox

    body = ("---\norigin: ms\nkey: O-09\nrev: 1\n---\n\n"
            "token ghp_16C7e42F292c6912E7710c838347Ae178B4a\n").encode()
    entry = {"kind": "B", "origin": "ms", "key": "O-09", "rev": 1,
             "hash": hashlib.sha256(body).hexdigest(), "path": "peers/ms/O-09.md"}

    assert inbox.accept("ms", entry, body) is False


def test_the_block_log_records_the_rule_but_not_the_node(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    from aiforge_core.memory.sync import status

    status.record_block("O-02", "secrets.aws_key", "shaped like an aws key")
    rows = status.blocks()
    assert rows[0]["rule"] == "secrets.aws_key"
    assert "AKIA" not in str(rows)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_redact.py -v -k "advertised or re_runs or block_log"
```

Expected: FAIL — the blocked node is pushed and `status` does not exist.

- [ ] **Step 3: Implement**

In `push.py`, `_mine` becomes the gate. Split the filtering into its own
function so `_mine` stays one idea per function:

```python
def _permitted(entries: list[dict], root, _io) -> tuple[list[dict], dict]:
    """The entries the outbound filter allows, and a count per rule.

    Run HERE, at the offer, rather than at the send: an entry that never enters
    the offer is one the admin never learns exists, which is the difference
    between "we chose not to send it" and "we told them about it and then
    declined".

    A node that cannot be read is dropped rather than sent — the filter has to
    see the text to vouch for it, and an unreadable file will be offered again
    next cycle once it can be read.
    """
    from aiforge_core.memory.okf import nodes
    from aiforge_core.memory.sync import redact, status

    kept: list[dict] = []
    blocked: dict[str, int] = {}
    for entry in entries:
        path = root / str(entry.get("path") or "")
        try:
            node = nodes.parse_node(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            blocked["unreadable"] = blocked.get("unreadable", 0) + 1
            continue
        verdict = redact.review(node)
        if verdict.send:
            kept.append(entry)
            continue
        blocked[verdict.rule] = blocked.get(verdict.rule, 0) + 1
        status.record_block(str(entry.get("key") or ""), verdict.rule, verdict.reason)
    if blocked:
        _log.info("sync: filter held back %d node(s): %s",
                  sum(blocked.values()), blocked)
    return kept, blocked
```

`_mine` calls it last, and `run_once` records the counts:

```python
        entries, blocked = _permitted(_mine(manifest.build()), _io.root(), _io)
        result["offered"] = len(entries)
        result["blocked"] = sum(blocked.values())
        result["blocked_by_rule"] = blocked
        want = transport.offer(base_url, entries, group=group)
        if want is None:
            return result
        result["ok"] = True
        result["pending"] = len(want)
```

In `inbox.accept`, add the same check after the `derived` refusal:

```python
    from aiforge_core.memory.okf import nodes
    from aiforge_core.memory.sync import redact

    if entry.get("kind") == "B":
        try:
            verdict = redact.review(nodes.parse_node(body.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            verdict = redact.Verdict(False, "filter.unreadable",
                                     "the pushed body could not be parsed")
        if not verdict.send:
            # Defence in depth. The client is supposed to have filtered this
            # already; a client build that predates the filter has not, and the
            # admin is where such a node would become every other machine's
            # problem.
            _log.warning("sync: refusing pushed node %s from %s: %s",
                         entry.get("key"), peer_id or "<unattributed>", verdict.rule)
            return False
```

The block ring lives in `status.py`, written in Task 10; add it there now:

```python
# How many block decisions to keep. Enough to see a pattern across a few days
# of cycles, small enough that the file stays a glance rather than a report.
MAX_BLOCKS = 200


def record_block(key: str, rule: str, reason: str) -> None:
    """Note that one node was held back. Never raises, never stores the node.

    The KEY and the RULE, never the text: this file is written to disk, and a
    log that records the secret it caught is the leak it was meant to prevent.
    """
    import time

    try:
        rec = _io.read_json(_blocks_path())
        rows = [r for r in (rec.get("blocks") or []) if isinstance(r, dict)]
        rows.append({"key": str(key), "rule": str(rule), "reason": str(reason),
                     "at": int(time.time())})
        _io.write_json(_blocks_path(), {"blocks": rows[-MAX_BLOCKS:]})
    except Exception as exc:  # noqa: BLE001 — bookkeeping is not the payload
        _log.info("sync: could not record a filter block: %s", exc)


def blocks() -> list[dict]:
    """Every recorded block, most recent first."""
    rows = _io.read_json(_blocks_path()).get("blocks") or []
    return list(reversed([r for r in rows if isinstance(r, dict)]))
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/ -v
```

Expected: PASS. Pre-existing cycle tests may now push fewer nodes — if one
fails, its fixture body has no project signal; fix the fixture through
`_hub.author`, not the filter.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/push.py aiforge_core/memory/sync/inbox.py \
        aiforge_core/memory/sync/status.py tests/python/memory/sync/test_redact.py
git commit -m "feat(sync): filter at the offer, re-check at the admin, log what was held back"
```

---

## Task 10: Status, and staying quiet when the admin is down

**Files:**
- Create: `aiforge_core/memory/sync/status.py` (completing the file started in Task 9)
- Modify: `aiforge_core/memory/sync/transport.py` (failure logging)
- Test: `tests/python/memory/sync/test_status_quiet.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_status_quiet.py`:

```python
"""An admin that is down is normal operation, and must read like it.

Before this, every failed cycle logged a line forever: a laptop away from the
office for a week produced ~340 identical lines and no way to tell "the admin is
off" from "this machine is broken".
"""
from __future__ import annotations

import logging

import pytest

from aiforge_core.memory.sync import status


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    status.reset()


def test_the_record_round_trips():
    status.record(state="ok", admin="http://nuc:8799", reachable=True,
                  group="cellular", groups_available=["cellular", "retail"],
                  pending=3, pushed=12)
    row = status.read()
    assert row["state"] == "ok"
    assert row["group"] == "cellular"
    assert row["pending"] == 3
    assert row["last_ok"]


def test_pending_falls_to_zero_when_everything_is_sent():
    status.record(state="ok", admin="a", reachable=True, group="", pending=5)
    status.record(state="ok", admin="a", reachable=True, group="", pending=0)
    assert status.read()["pending"] == 0


def test_a_failure_keeps_the_last_good_timestamp():
    status.record(state="ok", admin="a", reachable=True, group="")
    was = status.read()["last_ok"]
    status.record(state="unreachable", admin="a", reachable=False, group="",
                  error="ConnectError: refused")
    row = status.read()
    assert row["last_ok"] == was
    assert row["last_error"] == "ConnectError: refused"


def test_repeated_failures_log_once_not_once_per_cycle(caplog):
    caplog.set_level(logging.WARNING, logger="aiforge.sync")
    for _ in range(20):
        status.note_failure("http://nuc:8799", "ConnectError: refused")
    assert len([r for r in caplog.records if "unreachable" in r.message]) == 1


def test_recovery_logs_exactly_one_line(caplog):
    status.note_failure("http://nuc:8799", "ConnectError: refused")
    caplog.set_level(logging.INFO, logger="aiforge.sync")
    status.note_success("http://nuc:8799")
    status.note_success("http://nuc:8799")
    assert len([r for r in caplog.records if "reachable again" in r.message]) == 1


def test_a_new_error_after_an_hour_logs_again(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="aiforge.sync")
    clock = [1000.0]
    monkeypatch.setattr(status.time, "monotonic", lambda: clock[0])
    status.note_failure("a", "boom")
    clock[0] += status.QUIET_SECONDS + 1
    status.note_failure("a", "boom")
    assert len([r for r in caplog.records if "unreachable" in r.message]) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_status_quiet.py -v
```

Expected: FAIL — `status.record` / `note_failure` / `reset` do not exist.

- [ ] **Step 3: Implement**

Complete `aiforge_core/memory/sync/status.py` (the block ring from Task 9 stays):

```python
"""What sync is doing, in one record — and how it stays quiet while it cannot.

Two jobs that belong together because they are two views of one fact:

* **The record.** One JSON file the settings screen reads. It is written every
  cycle and is the ONLY place a UI has to look to answer "is this syncing, with
  whom, into which group, and what is it waiting on".
* **The quiet.** An unreachable admin is ordinary — a laptop off the LAN, a hub
  being rebooted — and before this every failed cycle logged a line. A machine
  away for a week produced hundreds of identical lines, and none of them
  distinguished "the admin is off" from "this machine is broken". Now the state
  CHANGE is what logs.

``pending`` deserves its own note. It is not a queue: it is the length of the
offer's ``want`` list, recomputed from the tree every cycle. A successful push
makes the entry no longer wanted, so pending falls to zero by construction —
there is no outbox to drain, drift, or clear by hand.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from aiforge_core.config.paths import config_dir
from aiforge_core.memory.sync import _io

_log = logging.getLogger("aiforge.sync")

_FILE = "sync_status.json"
_BLOCKS_FILE = "sync_blocks.json"

# How long a *continuing* outage stays silent before one line records that it is
# still out and for how long. An hour is long enough that a day offline is 24
# lines rather than 120, short enough that a log covering a working day still
# shows the outage.
QUIET_SECONDS = 3600.0

# Per-admin: (last_error, when_it_was_logged). Process state, not persisted —
# a restart logging one line about a still-down admin is correct.
_LAST: dict[str, tuple[str, float]] = {}


def _path() -> Path:
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / _FILE


def _blocks_path() -> Path:
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / _BLOCKS_FILE


def reset() -> None:
    """Drop the process-local quiet state. For tests and for a role change."""
    _LAST.clear()


def read() -> dict:
    return _io.read_json(_path())


def record(*, state: str, admin: str, reachable: bool, group: str = "",
           groups_available: list[str] | None = None, pending: int = 0,
           pushed: int = 0, error: str | None = None) -> dict:
    """Write this cycle's record. Never raises.

    ``last_ok`` is preserved across a failure: "we have not synced since 14:02"
    is the question an operator actually asks, and overwriting it on every
    failed cycle destroys the only answer.
    """
    prev = read()
    row = {
        "state": state,
        "admin": admin,
        "group": group,
        "groups_available": list(groups_available or prev.get("groups_available") or []),
        "reachable": bool(reachable),
        "pending": int(pending),
        "pushed_total": int(prev.get("pushed_total") or 0) + int(pushed),
        "blocked": _block_counts(),
        "last_ok": int(time.time()) if reachable else prev.get("last_ok"),
        "last_error": error if error is not None else (
            None if reachable else prev.get("last_error")),
        "at": int(time.time()),
    }
    try:
        _io.write_json(_path(), row)
    except Exception as exc:  # noqa: BLE001 — a status write must not fail a cycle
        _log.info("sync: could not write the status record: %s", exc)
    return row


def _block_counts() -> dict:
    counts: dict[str, int] = {}
    for r in blocks():
        rule = str(r.get("rule") or "")
        counts[rule] = counts.get(rule, 0) + 1
    return counts


def note_failure(admin: str, error: str) -> None:
    """One cycle could not reach ``admin``. Logs only on a change.

    The first failure logs at WARNING. After that the same error is silent
    until ``QUIET_SECONDS`` have passed, at which point one line records that it
    is still down and for how long. A DIFFERENT error logs immediately — the
    admin going from "connection refused" to "401" is news.
    """
    now = time.monotonic()
    seen, at = _LAST.get(admin, ("", 0.0))
    if seen == error and (now - at) < QUIET_SECONDS:
        return
    if seen == error:
        _log.warning("sync: admin %s still unreachable after %.0f minutes: %s",
                     admin, (now - at) / 60.0, error)
    else:
        _log.warning("sync: admin %s is unreachable: %s", admin, error)
    _LAST[admin] = (error, now)


def note_success(admin: str) -> None:
    """One cycle reached ``admin``. Logs only the transition back."""
    if admin in _LAST:
        _log.info("sync: admin %s is reachable again", admin)
        _LAST.pop(admin, None)


MAX_BLOCKS = 200
# ... record_block() and blocks() from Task 9 follow here ...

__all__ = ["QUIET_SECONDS", "MAX_BLOCKS", "reset", "read", "record",
           "note_failure", "note_success", "record_block", "blocks"]
```

In `transport.py`, route the four `_log.info("sync: … failed …")` calls in
`offer`, `push_blob`, `fetch_manifest`, `fetch_blob` and `fetch_groups` through
`status.note_failure(base_url, f"{type(exc).__name__}: {exc}")`, and call
`status.note_success(base_url)` on the success path of `fetch_manifest`. Import
`status` lazily inside each function, matching the file's existing style.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_status_quiet.py tests/python/memory/sync/test_transport_limits.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/status.py aiforge_core/memory/sync/transport.py \
        tests/python/memory/sync/test_status_quiet.py
git commit -m "feat(sync): one status record, and one log line per outage instead of one per cycle"
```

---

## Task 11: Serve the status

**Files:**
- Modify: `aiforge_core/api/routes/sync.py`
- Test: `tests/python/memory/sync/test_sync_routes.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_status_route_serves_the_record(client, tmp_path, monkeypatch):
    from aiforge_core.memory.sync import status

    status.record(state="ok", admin="http://nuc:8799", reachable=True,
                  group="cellular", pending=2)
    r = client.get("/api/memory/sync/status")
    assert r.status_code == 200
    assert r.json()["group"] == "cellular"
    assert r.json()["pending"] == 2


def test_status_route_on_a_machine_that_has_never_synced(client):
    r = client.get("/api/memory/sync/status")
    assert r.status_code == 200
    assert r.json()["state"] in ("unknown", "no-admin")
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_sync_routes.py -v -k status
```

Expected: FAIL — 404.

- [ ] **Step 3: Implement**

In `aiforge_core/api/routes/sync.py`:

```python
@router.get("/api/memory/sync/status")
def sync_status() -> dict:
    """Everything the settings screen needs, in one call.

    Served from the record the cycle writes rather than probed live: a UI must
    not be the thing that discovers an admin is down, because a probe on a page
    load is a 20-second hang the moment it is.
    """
    from aiforge_core.memory.sync import group, redact, role, status

    row = dict(status.read())
    row.setdefault("state", "no-admin" if not role.admin_url() else "unknown")
    row["role"] = role.role()
    row.setdefault("admin", role.admin_url())
    row.setdefault("group", group.selected())
    row["rules"] = redact.explain()
    row["recent_blocks"] = status.blocks()[:20]
    return row
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_sync_routes.py -v
```

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/api/routes/sync.py tests/python/memory/sync/test_sync_routes.py
git commit -m "feat(sync): serve the status record"
```

---

## Task 12: Snapshots and revert

**Files:**
- Create: `aiforge_core/memory/sync/snapshot.py`
- Modify: `aiforge_core/api/routes/groups.py`
- Modify: `aiforge_core/memory/okf/tiers.py` (snapshot before a fold)
- Modify: `aiforge_core/memory/sync/loop.py` (snapshot `mesh/` before a pull applies)
- Test: `tests/python/memory/sync/test_snapshot.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/memory/sync/test_snapshot.py`:

```python
"""Snapshots and revert.

Hardlinks, so a snapshot of a tree of markdown notes costs inodes and no bytes,
which is what makes "snapshot before every fold" affordable.
"""
from __future__ import annotations

import pytest

from aiforge_core.memory.sync import _io, snapshot


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    root = _io.root()
    (root / "mesh" / "nuc").mkdir(parents=True, exist_ok=True)
    (root / "mesh" / "nuc" / "M-01.md").write_text("one", encoding="utf-8")
    return root


def test_take_creates_a_listed_snapshot(tree):
    stamp = snapshot.take(tree, "2026-08-26T100000Z")
    assert stamp in [s["stamp"] for s in snapshot.listing(tree)]
    assert (tree / snapshot.DIR / stamp / "mesh" / "nuc" / "M-01.md").read_text() == "one"


def test_a_snapshot_is_hardlinked_not_copied(tree):
    stamp = snapshot.take(tree, "2026-08-26T100000Z")
    a = (tree / "mesh" / "nuc" / "M-01.md").stat()
    b = (tree / snapshot.DIR / stamp / "mesh" / "nuc" / "M-01.md").stat()
    assert (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def test_revert_restores_the_snapshotted_content(tree):
    stamp = snapshot.take(tree, "2026-08-26T100000Z")
    (tree / "mesh" / "nuc" / "M-01.md").write_text("two", encoding="utf-8")
    (tree / "mesh" / "nuc" / "M-02.md").write_text("new", encoding="utf-8")

    snapshot.revert(tree, stamp)

    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "one"
    assert not (tree / "mesh" / "nuc" / "M-02.md").exists()


def test_revert_snapshots_the_current_state_first(tree):
    """A revert must itself be revertible — one wrong call cannot destroy state."""
    first = snapshot.take(tree, "2026-08-26T100000Z")
    (tree / "mesh" / "nuc" / "M-01.md").write_text("two", encoding="utf-8")

    snapshot.revert(tree, first, stamp="2026-08-26T110000Z")

    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "one"
    snapshot.revert(tree, "2026-08-26T110000Z", stamp="2026-08-26T120000Z")
    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "two"


def test_pruning_keeps_the_newest_n(tree, monkeypatch):
    monkeypatch.setenv("AIFORGE_SYNC_SNAPSHOTS", "3")
    for i in range(6):
        snapshot.take(tree, f"2026-08-26T10000{i}Z")
    assert [s["stamp"] for s in snapshot.listing(tree)] == [
        "2026-08-26T100005Z", "2026-08-26T100004Z", "2026-08-26T100003Z"]


def test_reverting_to_an_unknown_stamp_raises_and_changes_nothing(tree):
    with pytest.raises(FileNotFoundError):
        snapshot.revert(tree, "nope")
    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "one"


def test_a_snapshot_is_never_advertised(tree):
    """`.snapshots` is dotted, so `_io._hidden_below` already excludes it —
    this test is what stops somebody renaming it to something undotted."""
    from aiforge_core.memory.sync import manifest

    snapshot.take(tree, "2026-08-26T100000Z")
    assert all(snapshot.DIR not in e["path"] for e in manifest.build())
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_snapshot.py -v
```

Expected: FAIL — no module `snapshot`.

- [ ] **Step 3: Implement**

Create `aiforge_core/memory/sync/snapshot.py`:

```python
"""Revert points for a memory tree.

Cheap enough to take before every fold, because the tree is small markdown files
and a hardlink copy costs inodes rather than bytes. That affordability is the
whole design: a snapshot somebody has to remember to take is one nobody has.

Lives in ``.snapshots`` — DOTTED, deliberately. ``_io._hidden_below`` already
excludes a dotted directory below a scanned root from the manifest, so a
snapshot can never be advertised to a peer, served over ``/blob`` or re-planted
somewhere else. Renaming this constant to something undotted would silently
replicate every revert point to the whole fleet.

A revert **snapshots the current state before it restores**, so a revert is
itself revertible. An operator who reverts to the wrong stamp has made a
recoverable mistake rather than destroyed the state they meant to keep.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

_log = logging.getLogger("aiforge.sync")

DIR = ".snapshots"

# Directories worth a revert point: the received inbox and the fold. ``okf/`` is
# authored by hand and is never written by sync, so it is not this feature's to
# roll back — reverting it would destroy work sync never touched.
SUBTREES = ("peers", "mesh")

_DEFAULT_KEEP = 10


def keep() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_SYNC_SNAPSHOTS") or _DEFAULT_KEEP))
    except ValueError:
        return _DEFAULT_KEEP


def _dir(root: Path) -> Path:
    return Path(root) / DIR


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())


def take(root: Path, stamp: str = "") -> str:
    """Snapshot ``root``'s syncable subtrees. Returns the stamp. Never raises.

    Hardlinked: the snapshot shares every file's inode with the live tree, so it
    is near-free in space and instant in time. Safe because every writer in this
    codebase writes atomically (``_io.write_atomic`` stages a ``.tmp`` and
    renames), so a later write REPLACES the directory entry and never mutates
    the inode the snapshot holds.
    """
    root = Path(root)
    stamp = stamp or _stamp()
    target = _dir(root) / stamp
    try:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        for name in SUBTREES:
            src = root / name
            if src.is_dir():
                shutil.copytree(src, target / name, copy_function=os.link,
                                dirs_exist_ok=True)
        _prune(root)
    except OSError as exc:
        # A snapshot that cannot be taken must not stop the fold it precedes:
        # the fold is the product, the revert point is insurance.
        _log.warning("sync: could not snapshot %s: %s", root, exc)
    return stamp


def listing(root: Path) -> list[dict]:
    """Every snapshot, newest first."""
    d = _dir(Path(root))
    if not d.is_dir():
        return []
    rows = []
    for p in d.iterdir():
        if not p.is_dir():
            continue
        rows.append({"stamp": p.name,
                     "files": sum(1 for _ in p.rglob("*") if _.is_file())})
    return sorted(rows, key=lambda r: r["stamp"], reverse=True)


def _prune(root: Path) -> None:
    for row in listing(root)[keep():]:
        try:
            shutil.rmtree(_dir(Path(root)) / row["stamp"])
        except OSError as exc:
            _log.info("sync: could not prune snapshot %s: %s", row["stamp"], exc)


def revert(root: Path, stamp: str, *, stamp_current: str = "", **kw) -> str:
    """Restore ``stamp`` over ``root``. Returns the stamp of the state replaced.

    Raises ``FileNotFoundError`` for an unknown stamp, BEFORE anything is
    touched: a revert that half-applies is worse than one that refuses.
    """
    root = Path(root)
    stamp_current = stamp_current or kw.get("stamp") or _stamp()
    source = _dir(root) / stamp
    if not source.is_dir():
        raise FileNotFoundError(f"no such snapshot: {stamp}")

    replaced = take(root, stamp_current)
    for name in SUBTREES:
        live = root / name
        if live.is_dir():
            shutil.rmtree(live)
        src = source / name
        if src.is_dir():
            shutil.copytree(src, live, copy_function=os.link, dirs_exist_ok=True)
    _log.info("sync: reverted %s to %s (previous state kept as %s)",
              root, stamp, replaced)
    return replaced


__all__ = ["DIR", "SUBTREES", "keep", "take", "listing", "revert"]
```

Note the `revert` signature: the test calls it as `revert(tree, first,
stamp="…")`, so `stamp` is accepted as a keyword alias for `stamp_current` —
keep both, and prefer `stamp_current` in new callers.

Call it from `tiers._distil_one`, immediately before `_run_tier`:

```python
    from aiforge_core.memory.sync import snapshot
    snapshot.take(_io.root())
```

and from `loop._pull`, immediately before `_fetch_wanted`:

```python
    if plan["want"]:
        # Only when something is actually about to change — an idle cycle must
        # not churn a revert point out of the window.
        from aiforge_core.memory.sync import _io, snapshot
        snapshot.take(_io.root())
```

Add the operator routes to `aiforge_core/api/routes/groups.py`:

```python
@router.get("/api/admin/groups/{name}/snapshots")
def group_snapshots(name: str) -> dict:
    from aiforge_core.memory.sync import _io, group, snapshot

    with _grouped(name):
        return {"group": name, "snapshots": snapshot.listing(_io.root())}


@router.post("/api/admin/groups/{name}/revert",
             responses={404: {"description": "No such group or snapshot"}})
async def group_revert(name: str, request: Request) -> dict:
    from aiforge_core.memory.sync import _io, snapshot

    payload = await request.json()
    to = str((payload or {}).get("to") or "").strip()
    with _grouped(name):
        try:
            replaced = snapshot.revert(_io.root(), to)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
    return {"group": name, "reverted_to": to, "previous_state": replaced}


def _grouped(name: str):
    """Scope to ``name``, or to the ungrouped tree when it is empty."""
    from aiforge_core.memory.sync import group

    name = (name or "").strip()
    if name and name not in group.known():
        raise HTTPException(404, f"no such group: {name}")
    return group.scoped(name)
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/python/memory/sync/test_snapshot.py -v
```

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/sync/snapshot.py aiforge_core/api/routes/groups.py \
        aiforge_core/memory/okf/tiers.py aiforge_core/memory/sync/loop.py \
        tests/python/memory/sync/test_snapshot.py
git commit -m "feat(sync): hardlink snapshots before every fold and pull, with revert"
```

---

## Task 13: The client's view rebuild becomes atomic

**Files:**
- Modify: `aiforge_core/memory/okf/tiers.py` (`build_view`)
- Test: `tests/python/memory/test_okf_view_recall.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_a_failed_view_build_leaves_the_previous_view_intact(tmp_path, monkeypatch):
    """view/ is what agents read. Half-old and half-new is worse than stale."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "ms")
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import _io, paths

    view = paths.view_dir()
    view.mkdir(parents=True, exist_ok=True)
    (view / "V-01.md").write_text("the good view", encoding="utf-8")

    okf = _io.root() / "okf"
    okf.mkdir(parents=True, exist_ok=True)
    (okf / "O-01.md").write_text(
        "---\norigin: ms\nkey: O-01\nrev: 1\n---\n\nnote in `x/y.py`\n",
        encoding="utf-8")

    def _boom(**kw):
        raise RuntimeError("the learner is down")

    monkeypatch.setattr(tiers, "_run_tier", _boom)
    tiers.build_view()

    assert (view / "V-01.md").read_text() == "the good view"
    assert not (paths.view_dir().parent / "view.tmp").exists()
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/python/memory/test_okf_view_recall.py -v -k failed_view
```

Expected: FAIL — either the exception escapes, or `view/` is left mangled.

- [ ] **Step 3: Implement**

In `tiers.build_view`, build into a sibling and swap:

```python
    # Built into a sibling and swapped, never in place. view/ is the working
    # knowledge every agent reads: a crash, an ENOSPC or a learner outage
    # part-way through an in-place rebuild left it half old and half new, which
    # is strictly worse than leaving yesterday's view alone. The swap is two
    # renames on the same filesystem, so there is no window where view/ is
    # absent for longer than a rename.
    from aiforge_core.memory.sync import paths

    final = paths.view_dir()
    staging = final.parent / "view.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        result = _run_tier(directory=staging, prefix="V", derived=VIEW,
                           inputs=inputs, role=role)
    except Exception as exc:  # noqa: BLE001 — a failed build keeps the old view
        shutil.rmtree(staging, ignore_errors=True)
        _log.warning("okf: view build failed, keeping the previous view: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}

    previous = final.parent / "view.old"
    shutil.rmtree(previous, ignore_errors=True)
    if final.exists():
        final.rename(previous)
    staging.rename(final)
    shutil.rmtree(previous, ignore_errors=True)
```

Add `import shutil` at the top of `tiers.py` if it is not already imported.
`view.tmp` and `view.old` sit beside `view/`, which `paths.node_roots()` does
not include, so neither is ever advertised.

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/python/memory/ -v
```

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/memory/okf/tiers.py tests/python/memory/test_okf_view_recall.py
git commit -m "fix(okf): rebuild the view into a sibling and swap, so a failed build keeps the old one"
```

---

## Task 14: `run.sh --admin-url` and `--group`

**Files:**
- Modify: `run.sh` (flag block near line 193, validation near line 252, banner near line 297)
- Test: `tests/python/test_run_sh_group_flags.py`

- [ ] **Step 1: Write the failing test**

Create `tests/python/test_run_sh_group_flags.py`, modelled on the existing
`tests/python/test_run_sh_admin_role.py` (read it first and reuse its harness —
it already knows how to run `run.sh` with a stub env file and a `--dry-run`-ish
early exit):

```python
"""``run.sh --admin-url`` and ``--group`` persist, and refuse the impossible."""
from __future__ import annotations

import subprocess


def test_admin_url_is_persisted(run_sh, env_file):
    run_sh("--admin-url", "http://nuc:8799")
    assert "AIFORGE_ADMIN_URL=http://nuc:8799" in env_file.read_text()


def test_admin_url_is_refused_on_the_admin_itself(run_sh, env_file):
    env_file.write_text("AIFORGE_ROLE=admin\n")
    out = run_sh("--admin-url", "http://nuc:8799", expect_fail=True)
    assert "cannot be both" in out


def test_group_is_persisted(run_sh, env_file):
    run_sh("--group", "cellular")
    assert "AIFORGE_SYNC_GROUP=cellular" in env_file.read_text()


def test_an_unusable_group_name_is_refused(run_sh):
    out = run_sh("--group", "../etc", expect_fail=True)
    assert "group name" in out
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/python/test_run_sh_group_flags.py -v
```

Expected: FAIL — `run.sh` treats `--admin-url` as an unknown flag.

- [ ] **Step 3: Implement**

In `run.sh`, in the flag `case` block near line 193:

```bash
    --admin-url) ADMIN_URL_SET="${2:-}"; shift ;;   # name the admin (this box is a spoke)
    --group)     GROUP_SET="${2:-}"; shift ;;       # preselect the sync group
```

Declare `ADMIN_URL_SET=""` and `GROUP_SET=""` beside the other flag variables
near line 166, and extend the `--help` block near line 53:

```
#   --admin-url <url>  name the memory admin this box syncs with (makes it a
#                spoke); persisted to the env file like --admin/--spoke
#   --group <name>  preselect the sync group. Only needed on a headless box
#                that will never see the settings screen — a client normally
#                discovers the list from the admin and picks one there.
```

After the existing `--admin`/`--spoke` validation near line 252:

```bash
if [[ -n "$ADMIN_URL_SET" ]]; then
  if [[ $ADMIN -eq 1 || "${AIFORGE_ROLE:-}" == "admin" ]]; then
    # Refused, not silently ignored — the same rule --admin already enforces in
    # the other direction. A box that is both stamps `derived: mesh` while also
    # pushing to somebody else's hub, and knowledge crosses in both directions.
    echo "error: --admin-url, but this box holds the admin role. A machine" >&2
    echo "       cannot be both. Run ./run.sh --spoke here first." >&2
    exit 2
  fi
  export AIFORGE_ADMIN_URL="$ADMIN_URL_SET"
  _write_env_line AIFORGE_ADMIN_URL "$ADMIN_URL_SET" \
    && echo "  memory: recorded AIFORGE_ADMIN_URL=$ADMIN_URL_SET in $_env_role_file"
fi

if [[ -n "$GROUP_SET" ]]; then
  if [[ ! "$GROUP_SET" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "error: '$GROUP_SET' is not a usable group name — it becomes a" >&2
    echo "       directory component, so it takes [A-Za-z0-9_-]." >&2
    exit 2
  fi
  export AIFORGE_SYNC_GROUP="$GROUP_SET"
  _write_env_line AIFORGE_SYNC_GROUP "$GROUP_SET" \
    && echo "  memory: recorded AIFORGE_SYNC_GROUP=$GROUP_SET in $_env_role_file"
fi
```

`_write_env_line` is a small generalisation of the existing `_write_role`
helper: same file, same locking, a `KEY=VALUE` pair instead of a fixed line.
Rewrite `_write_role admin` in terms of it so there is one writer.

Extend the banner near line 297 with the group and the last status:

```bash
  if [[ -n "${AIFORGE_SYNC_GROUP:-}" ]]; then
    echo "  memory: group $AIFORGE_SYNC_GROUP (pinned)"
  fi
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/python/test_run_sh_group_flags.py tests/python/test_run_sh_admin_role.py -v
bash -n run.sh
```

Expected: PASS, and `bash -n` silent.

- [ ] **Step 5: Commit**

```bash
git add run.sh tests/python/test_run_sh_group_flags.py
git commit -m "feat(run.sh): --admin-url and --group, persisted like --admin"
```

---

## Task 15: The settings panel

**Files:**
- Create: `web/src/views/Home.MemorySyncCard.tsx`
- Modify: `web/src/views/Home.tsx`

- [ ] **Step 1: Write the component**

`Home.tsx` is already 734 lines; this goes in its own file, matching the
`Memory.*Panel.tsx` split the codebase already uses.

Create `web/src/views/Home.MemorySyncCard.tsx`:

```tsx
/**
 * Memory sync settings.
 *
 * Everything here reads ONE endpoint (`/api/memory/sync/status`), which is
 * served from the record the sync cycle writes rather than probed live: a page
 * load must not be the thing that discovers the admin is down, because that
 * turns a render into a 20-second hang.
 */
import { useEffect, useState } from 'react'

type SyncStatus = {
  state: 'ok' | 'unreachable' | 'needs-group-selection' | 'group-unknown' | 'no-admin' | 'unknown'
  admin: string
  role: string
  group: string
  groups_available: string[]
  reachable: boolean
  pending: number
  pushed_total: number
  blocked: Record<string, number>
  last_ok: number | null
  last_error: string | null
  recent_blocks: { key: string; rule: string; reason: string; at: number }[]
}

const LABEL: Record<SyncStatus['state'], string> = {
  ok: 'Syncing',
  unreachable: 'Admin unreachable',
  'needs-group-selection': 'Choose a group',
  'group-unknown': 'Group not published by this admin',
  'no-admin': 'No admin configured',
  unknown: 'Not synced yet',
}

function when(ts: number | null): string {
  if (!ts) return 'never'
  return new Date(ts * 1000).toLocaleString()
}

export default function MemorySyncCard() {
  const [st, setSt] = useState<SyncStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  async function load() {
    try {
      const r = await fetch('/api/memory/sync/status')
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setSt(await r.json())
      setErr('')
    } catch (e) {
      setErr(String(e))
    }
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 30_000)
    return () => clearInterval(t)
  }, [])

  async function join(name: string) {
    setBusy(true)
    try {
      await fetch('/api/memory/sync/group', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ group: name }),
      })
      await load()
    } finally {
      setBusy(false)
    }
  }

  async function syncNow() {
    setBusy(true)
    try {
      await fetch('/api/memory/sync/now', { method: 'POST' })
      await load()
    } finally {
      setBusy(false)
    }
  }

  if (err) return <div className="card"><h3>Memory sync</h3><p className="muted">{err}</p></div>
  if (!st) return <div className="card"><h3>Memory sync</h3><p className="muted">Loading…</p></div>

  return (
    <div className="card">
      <h3>Memory sync</h3>

      <div className="row">
        <span className={`badge ${st.reachable ? 'ok' : 'warn'}`}>{LABEL[st.state]}</span>
        <span className="muted">
          {st.role === 'admin'
            ? 'this machine is the admin'
            : st.admin || 'no admin url set'}
        </span>
      </div>

      {/* An admin that is down is ordinary, so it reads as a fact, not an alarm. */}
      {!st.reachable && st.state !== 'no-admin' && (
        <p className="muted">Last synced {when(st.last_ok)}. {st.last_error}</p>
      )}

      {st.groups_available.length > 1 ? (
        <label>
          Group
          <select value={st.group} disabled={busy}
                  onChange={(e) => join(e.target.value)}>
            <option value="">— select a group —</option>
            {st.groups_available.map((g) => <option key={g} value={g}>{g}</option>)}
          </select>
        </label>
      ) : (
        <p className="muted">Group: {st.group || 'ungrouped'}</p>
      )}

      <dl className="stats">
        <dt>Waiting to send</dt><dd>{st.pending}</dd>
        <dt>Sent in total</dt><dd>{st.pushed_total}</dd>
        <dt>Held back</dt>
        <dd>{Object.values(st.blocked).reduce((a, b) => a + b, 0)}</dd>
      </dl>

      {st.recent_blocks.length > 0 && (
        <details>
          <summary>What was held back, and why</summary>
          <ul>
            {st.recent_blocks.map((b) => (
              <li key={`${b.key}-${b.at}`}>
                <code>{b.key}</code> — {b.reason} <span className="muted">({b.rule})</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      <button onClick={syncNow} disabled={busy || st.state === 'needs-group-selection'}>
        Sync now
      </button>
    </div>
  )
}
```

- [ ] **Step 2: Add the two routes the panel calls**

In `aiforge_core/api/routes/sync.py`:

```python
@router.put("/api/memory/sync/group", responses={400: {"description": "Bad name"}})
async def choose_group(request: Request) -> dict:
    """This client joins a group. Persisted, so the choice survives a restart."""
    payload = await _read_json(request)
    from aiforge_core.memory.sync import group

    try:
        return {"group": group.choose(str(payload.get("group") or ""))}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/api/memory/sync/now")
def sync_now() -> dict:
    """Run one cycle on demand. Returns the rows the cycle produced."""
    from aiforge_core.memory.sync import loop

    return {"rows": loop.run_once()}
```

- [ ] **Step 3: Render it**

In `web/src/views/Home.tsx`, import and place it after the integrations block:

```tsx
import MemorySyncCard from './Home.MemorySyncCard'
...
<MemorySyncCard />
```

- [ ] **Step 4: Build and lint**

```bash
cd web && npm run build && npx tsc --noEmit && cd ..
.venv/bin/python -m pytest tests/python/memory/sync/test_sync_routes.py -v
```

Expected: build succeeds, `tsc` silent, route tests pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/views/Home.MemorySyncCard.tsx web/src/views/Home.tsx \
        aiforge_core/api/routes/sync.py
git commit -m "feat(web): memory sync settings panel with group picker and filter log"
```

---

## Task 16: Full suite, SonarQube, merge

- [ ] **Step 1: Run the whole Python suite**

```bash
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -20
```

Expected: everything passes. `tests/python/api/test_access_log_filter.py::test_filter_is_idempotent` is a known order-dependent flake that also fails on `main` — verify it fails on `main` before dismissing it, and fix anything else.

- [ ] **Step 2: Build the frontend**

```bash
cd web && npm run build && cd ..
```

- [ ] **Step 3: Pre-filter cognitive complexity locally**

The scanner is ground truth, but a local pass catches the obvious ones first:

```bash
uv pip install --python .venv/bin/python cognitive_complexity
.venv/bin/python /tmp/cc.py aiforge_core/memory/sync aiforge_core/api/routes
```

Any function over 15 gets split before scanning. Note from prior work: the
package under-reports on dict-heavy returns, so treat ≤12 as the safe local
target rather than ≤15.

- [ ] **Step 4: Run the real scanner**

```bash
ssh nuc 'cd /home/ai/codeRepo/AIForgeCrew && git fetch origin && \
  git checkout feat/group-memory-sync && git reset --hard origin/feat/group-memory-sync'
ssh nuc 'docker run --rm --network host \
  -e SONAR_HOST_URL=http://localhost:9002 \
  -e SONAR_TOKEN=squ_f9414e377593af2248a5bc6ba3fb9aeb6fa5d0a6 \
  -v /home/ai/codeRepo/AIForgeCrew:/usr/src sonarsource/sonar-scanner-cli \
  -Dsonar.projectKey=aiforgecrew-group-sync \
  -Dsonar.sources=aiforge_core,scripts,web/src -Dsonar.tests=tests'
```

The branch must be pushed first for the NUC to fetch it.

- [ ] **Step 5: Read the findings and fix them**

```bash
ssh nuc 'curl -s -u squ_f9414e377593af2248a5bc6ba3fb9aeb6fa5d0a6: \
  "http://localhost:9002/api/issues/search?componentKeys=aiforgecrew-group-sync&resolved=false&ps=200&facets=rules,types,severities"' \
  | python3 -m json.tool | head -80
```

Fix every new finding on the files this branch touched. Known false-positive
classes from prior work (S5527/S4830 scoped TLS opt-outs, S2115, S5332 scheme
validation) are pre-existing and out of scope — do not edit them.

- [ ] **Step 6: Re-scan until clean, then merge**

```bash
git checkout main && git merge --no-ff feat/group-memory-sync \
  -m "Merge feat/group-memory-sync: group-scoped memory sync, outbound filtering, revert"
git push origin main
```

- [ ] **Step 7: Clean up the worktree**

```bash
git worktree remove .worktrees/feat/group-memory-sync
```

---

## Self-review notes

**Spec coverage:** groups → Tasks 3-6; filter → 7-9; untouchable trees → 1-2;
smoother merge → 13; revert → 12; status/pending/quiet → 10-11; entry points →
14-15; testing → embedded per task; done → 16.

**Known deferrals, stated rather than hidden:**
- The client's `mesh/` snapshot (Task 12) covers the pull. The client does not
  snapshot `okf/` — sync never writes it, so there is nothing there to revert.
- `explain()` returns stage-level docs, not per-rule ones. The settings panel
  shows rules through `recent_blocks`, which is what an operator actually reads.
- The entropy threshold is a first guess. It is env-tunable and logged by rule
  precisely so the first week of block logs sets it, not this document.
