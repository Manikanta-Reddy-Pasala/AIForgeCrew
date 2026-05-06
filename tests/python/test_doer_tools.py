"""Unit tests for the v6 Doer's filesystem + intelligence tools.

Network-free. Each test uses a per-test ``AIFORGE_REPO_ROOT`` so writes
are sandboxed inside a tmp_path and never touch the operator's real
workspace.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiforge_core.runtime import doer_tools as dt


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AIFORGE_DOER_SKIP_SYNTAX", raising=False)
    return tmp_path


# ─── _validate_syntax — direct unit checks ────────────────────────────


def test_validate_python_compile_ok() -> None:
    ok, err = dt.validate_syntax("a.py", "def f():\n    return 1\n")
    assert ok and err == ""


def test_validate_python_syntax_error() -> None:
    ok, err = dt.validate_syntax("a.py", "def f(:\n    pass\n")
    assert not ok
    assert "(" in err or "syntax" in err.lower()


def test_validate_unbalanced_braces() -> None:
    ok, err = dt.validate_syntax("x.java", "class X { void f() { }")
    assert not ok
    assert "{}" in err


def test_validate_unbalanced_parens() -> None:
    ok, err = dt.validate_syntax("x.go", "func f(int x { }")
    assert not ok


def test_validate_empty_rejected() -> None:
    ok, err = dt.validate_syntax("a.py", "")
    assert not ok
    assert "empty" in err


def test_validate_java_python_kwargs_rejected() -> None:
    src = "class X {\n  void f() {\n    helper(name = bar, value = baz);\n  }\n}\n"
    ok, err = dt.validate_syntax("X.java", src)
    assert not ok
    assert "kwargs" in err


def test_validate_java_annotation_with_eq_allowed() -> None:
    # @Bean(name = "x") looks like kwargs but is valid Java annotation.
    src = (
        "package a;\n"
        "@Bean(name = \"thing\")\n"
        "public class X {\n"
        "  public void f() {}\n"
        "}\n"
    )
    ok, err = dt.validate_syntax("X.java", src)
    assert ok, err


def test_validate_unknown_extension_passes_when_balanced() -> None:
    ok, err = dt.validate_syntax("notes.txt", "hello world\n")
    assert ok, err


# ─── file_write integration ───────────────────────────────────────────


def test_file_write_rejects_corrupt_python(tmp_path: Path) -> None:
    res = dt.file_write("broken.py", "def f(:\n  return 1\n")
    assert res["ok"] is False
    assert "syntax_invalid" in res["error"]
    # File NOT written.
    assert not (tmp_path / "broken.py").exists()


def test_file_write_writes_clean_python(tmp_path: Path) -> None:
    res = dt.file_write("good.py", "x = 1\n")
    assert res["ok"] is True
    assert (tmp_path / "good.py").read_text() == "x = 1\n"


def test_file_write_skip_syntax_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIFORGE_DOER_SKIP_SYNTAX", "1")
    res = dt.file_write("legacy.py", "def f(:\n  pass\n")
    # Bypassed — file IS written despite invalid syntax.
    assert res["ok"] is True
    assert (tmp_path / "legacy.py").exists()


def test_file_write_path_traversal_blocked() -> None:
    res = dt.file_write("../escape.txt", "nope")
    assert res["ok"] is False
    assert "outside" in res["error"].lower() or "permission" in res["error"].lower()


# ─── memory_lookup ─────────────────────────────────────────────────────


def test_memory_lookup_handles_missing_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When unified_query raises, the tool returns ok=False with a clean
    error string instead of bubbling — the agent loop must not crash on
    a flaky memory backend."""
    import sys

    class _Boom:
        @staticmethod
        def query(*a, **kw):  # noqa: D401
            raise RuntimeError("backend down")

    monkeypatch.setitem(sys.modules, "aiforge_core.memory.unified_query", _Boom)
    res = dt.memory_lookup("anything")
    assert res["ok"] is False
    assert "backend down" in res["error"]


def test_memory_lookup_caps_k() -> None:
    """k > 12 should be clamped — guards against the model passing huge
    values that would dump the entire memory bank into the context."""
    import sys

    class _Stub:
        @staticmethod
        def query(text, **kw):
            return {
                "hits": [
                    {"source": "m", "score": 0.5, "text": f"row{i}"}
                    for i in range(20)
                ],
                "used_sources": ["memory"],
            }

    sys.modules["aiforge_core.memory.unified_query"] = _Stub  # type: ignore[assignment]
    try:
        res = dt.memory_lookup("query", k=999)
        assert res["ok"] is True
        assert len(res["hits"]) <= 12
    finally:
        sys.modules.pop("aiforge_core.memory.unified_query", None)
