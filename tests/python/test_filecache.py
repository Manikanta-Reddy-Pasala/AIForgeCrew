"""mtime-keyed JSON cache for hot config files (pipeline-efficiency #1)."""
from __future__ import annotations

import json
import os

from aiforge_core.config import _filecache as fc


def _write(p, obj):
    with open(p, "w") as f:
        json.dump(obj, f)


def test_parses_and_returns(tmp_path):
    fc.clear()
    p = tmp_path / "c.json"
    _write(p, {"a": 1})
    assert fc.read_json(p) == {"a": 1}


def test_returns_deepcopy_not_shared(tmp_path):
    fc.clear()
    p = tmp_path / "c.json"
    _write(p, {"a": {"b": 1}})
    first = fc.read_json(p)
    first["a"]["b"] = 999          # mutate the returned object
    second = fc.read_json(p)
    assert second["a"]["b"] == 1   # cache not corrupted


def test_missing_file_returns_none(tmp_path):
    fc.clear()
    assert fc.read_json(tmp_path / "nope.json") is None


def test_corrupt_json_returns_none(tmp_path):
    fc.clear()
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert fc.read_json(p) is None


def test_cache_busts_on_mtime_change(tmp_path):
    fc.clear()
    p = tmp_path / "c.json"
    _write(p, {"v": 1})
    assert fc.read_json(p)["v"] == 1
    # rewrite with a strictly newer mtime so the cache must re-parse
    _write(p, {"v": 2})
    os.utime(p, (10**9 + 5, 10**9 + 5))   # force a distinct mtime
    assert fc.read_json(p)["v"] == 2


def test_cache_hit_skips_reparse(tmp_path, monkeypatch):
    fc.clear()
    p = tmp_path / "c.json"
    _write(p, {"v": 1})
    fc.read_json(p)                         # populate
    calls = {"n": 0}
    real = json.loads
    monkeypatch.setattr(fc.json, "loads",
                        lambda s: (calls.__setitem__("n", calls["n"] + 1)
                                   or real(s)))
    fc.read_json(p)                         # same mtime → cache hit
    assert calls["n"] == 0                  # no re-parse
