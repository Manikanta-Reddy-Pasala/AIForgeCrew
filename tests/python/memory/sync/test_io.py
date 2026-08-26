"""Disk primitives. Every other sync module builds on exactly these."""
from __future__ import annotations

import hashlib
import threading

import pytest


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


def test_write_atomic_never_publishes_a_mixture_of_concurrent_writes(monkeypatch, tmp_path):
    """Concurrent writers of ONE target must not tear.

    With a fixed ``<target>.tmp`` staging name every writer shares one staging
    file: one truncates while another is mid-write, and the rename publishes a
    blend neither asked for — or fails outright because its temp file was
    renamed away. Measured against the pre-fix body, 58 of 100 rounds of exactly
    this race published torn content. The bodies differ in *length* as well as
    content; equal-length bodies hide the fault, because a single large write to
    a regular file usually does not interleave.
    """
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    target = tmp_path / "contended.md"
    bodies = [bytes(str(i), "ascii") * (40_000 * i) for i in range(1, 7)]
    start = threading.Barrier(len(bodies))
    failures: list[BaseException] = []

    def _write(body: bytes) -> None:
        start.wait()
        try:
            _io.write_atomic(target, body)
        except BaseException as exc:          # a losing writer must not blow up
            failures.append(exc)

    threads = [threading.Thread(target=_write, args=(b,)) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert failures == []
    assert target.read_bytes() in bodies      # exactly one whole body, never a blend
    assert list(tmp_path.glob("*.tmp")) == []


def test_read_node_meta_soft_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io

    good = tmp_path / "L-07.md"
    good.write_text('---\ntype: learning\nid: "L-07"\norigin: "nuc"\n---\n\nbody\n',
                    encoding="utf-8")
    bare = tmp_path / "bare.md"
    bare.write_text("no frontmatter here\n", encoding="utf-8")

    assert _io.read_node_meta(good).get("origin") == "nuc"
    assert _io.read_node_meta(bare) == {}
    assert _io.read_node_meta(tmp_path / "absent.md") == {}


def test_root_is_cached_per_memory_dir(monkeypatch, tmp_path):
    """A read path must not mkdir per call, and must still follow the env."""
    from aiforge_core.memory.sync import _io

    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "one"))
    first = _io.root()
    assert _io.root() is first                    # cached, no second mkdir

    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "two"))
    assert _io.root() == tmp_path / "two"         # ...but a switched peer is seen
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "one"))
    assert _io.root() == first


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


# ── the group scope ──────────────────────────────────────────────────────

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
    """Two concurrent tasks in different groups do not see each other's root.

    This is the whole reason the override is a ContextVar rather than an env
    var: the API serves requests concurrently, and one group writing into
    another's tree is the failure group isolation exists to prevent.
    """
    import asyncio

    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "base"))
    from aiforge_core.memory.sync import _io

    seen: dict = {}

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


# ── the authored tree ────────────────────────────────────────────────────

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
