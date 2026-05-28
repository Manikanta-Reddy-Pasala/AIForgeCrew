"""Tests for symbol push-on-change trigger (gap A8b)."""
from __future__ import annotations

import os

from aiforge_core.indexing.symbol_embed import should_refresh, request_refresh


def test_should_refresh_true_for_python():
    assert should_refresh(["src/foo.py"]) is True


def test_should_refresh_true_for_java_among_others():
    assert should_refresh(["README.md", "src/Foo.java"]) is True


def test_should_refresh_true_for_ts_go_rs():
    assert should_refresh(["a.ts"]) is True
    assert should_refresh(["b.go"]) is True
    assert should_refresh(["c.rs"]) is True


def test_should_refresh_false_for_non_code():
    assert should_refresh(["README.md", "notes.txt", "img.png"]) is False


def test_should_refresh_false_for_empty():
    assert should_refresh([]) is False


def test_should_refresh_custom_exts():
    assert should_refresh(["app.kt"], exts=(".kt",)) is True
    assert should_refresh(["app.py"], exts=(".kt",)) is False


def test_request_refresh_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_SYMBOL_PUSH_REFRESH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    out = request_refresh(["src/foo.py"])
    assert out["requested"] is False


def test_request_refresh_no_code_paths_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_SYMBOL_PUSH_REFRESH", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    out = request_refresh(["README.md"])
    assert out["requested"] is False


def test_request_refresh_writes_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_SYMBOL_PUSH_REFRESH", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    out = request_refresh(["src/foo.py", "README.md"])
    assert out["requested"] is True
    marker = out["marker"]
    assert os.path.isfile(marker)
