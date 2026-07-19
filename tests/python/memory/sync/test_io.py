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
