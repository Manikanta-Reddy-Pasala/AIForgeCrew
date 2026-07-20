"""The one atomic-publish primitive, and the callers that were converted to it.

The bug this guards is *staging*, not the rename: ``os.replace`` is atomic, but
a fixed ``<target>.tmp`` name is shared, so two writers scribble over each
other's staging file and the rename publishes a blend. Only varied-length
bodies expose it — equal-length bodies scored 0/20 while the varied ones scored
58/100 torn rounds against the pre-fix code.
"""
from __future__ import annotations

import json
import os
import threading

import pytest

from aiforge_core.config import _atomic


def _leftovers(directory) -> list:
    return [p.name for p in directory.iterdir() if p.name.startswith(".") or
            p.name.endswith(".tmp")]


def test_write_bytes_creates_parents_and_leaves_no_temp(tmp_path):
    target = tmp_path / "deep" / "nested" / "f.json"
    _atomic.write_bytes(target, b"body")

    assert target.read_bytes() == b"body"
    assert _leftovers(target.parent) == []


def test_write_text_round_trips_and_replaces(tmp_path):
    target = tmp_path / "f.txt"
    _atomic.write_text(target, "old")
    _atomic.write_text(target, "néw")

    assert target.read_text(encoding="utf-8") == "néw"


def test_published_file_keeps_ordinary_permissions(tmp_path):
    """mkstemp stages at 0600; every converted caller used a plain open, so the
    published file must still carry the umask default or their configs would
    silently tighten."""
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    target = tmp_path / "atomic.txt"
    _atomic.write_text(target, "x")

    assert os.stat(target).st_mode & 0o777 == os.stat(plain).st_mode & 0o777


def test_concurrent_writers_never_publish_a_blend(tmp_path):
    """Six threads off one barrier, distinct-length bodies, one target."""
    target = tmp_path / "contended.json"
    bodies = [bytes(str(i), "ascii") * (40_000 * i) for i in range(1, 7)]
    start = threading.Barrier(len(bodies))
    failures: list[BaseException] = []

    def _write(body: bytes) -> None:
        start.wait()
        try:
            _atomic.write_bytes(target, body)
        except BaseException as exc:          # a losing writer must not blow up
            failures.append(exc)

    threads = [threading.Thread(target=_write, args=(b,)) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert failures == []
    assert target.read_bytes() in bodies      # one whole body, never a mixture
    assert _leftovers(tmp_path) == []


def test_failed_write_leaves_no_temp_and_reraises(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_bytes(b"previous")

    def _boom(*a, **k):
        raise OSError("ENOSPC")

    monkeypatch.setattr(_atomic.os, "replace", _boom)
    with pytest.raises(OSError, match="ENOSPC"):
        _atomic.write_bytes(target, b"new")

    assert target.read_bytes() == b"previous"
    assert _leftovers(tmp_path) == []


# ───────────────────────── converted JSON callers ─────────────────────────
# The pre-change bytes are spelled out literally: an accidental ``indent=``
# drift would churn every config file on disk without any test noticing.

def test_filecache_write_json_is_byte_identical(tmp_path, monkeypatch):
    from aiforge_core.config import _filecache

    p = tmp_path / "sub" / "cfg.json"
    data = {"b": 1, "a": [2, 3]}
    _filecache.write_json(p, data)

    assert p.read_bytes() == json.dumps(data, indent=2).encode("utf-8")
    assert _filecache.read_json(p) == data


def test_mcp_and_model_registry_save_indent_2(tmp_path, monkeypatch):
    from aiforge_core.config import mcp_registry, model_registry

    rows = [{"name": "x", "url": "u"}]
    for mod in (mcp_registry, model_registry):
        p = tmp_path / f"{mod.__name__.rsplit('.', 1)[-1]}.json"
        monkeypatch.setattr(mod, "_path", lambda p=p: str(p))
        mod._save(rows)
        assert p.read_bytes() == json.dumps(rows, indent=2).encode("utf-8")
        assert mod._load() == rows


def test_integrations_set_writes_indent_2(tmp_path, monkeypatch):
    from aiforge_core.config import integrations

    p = tmp_path / "integrations.json"
    monkeypatch.setattr(integrations, "_path", lambda: p)
    integrations.set_("jira", {"url": "https://j", "token": "t"})

    expected = {"jira": {"url": "https://j", "token": "t"}}
    assert p.read_bytes() == json.dumps(expected, indent=2).encode("utf-8")
    assert integrations.load_all() == expected
