"""Toolchain probe: discovered interpreter is cached + injected, never
re-discovered.

The static python default is ``python -m pytest`` — on a python3-only
host that makes the Doer run ``python`` (fails) then re-search for
``python3`` every ticket. resolve_toolchain() must pin the real tool.
Pure stdlib import (neo4j is imported lazily inside funcs), runs local.
"""
from __future__ import annotations

import pytest

from aiforge_core.config import repo_standards as rs


@pytest.fixture(autouse=True)
def _clear():
    rs._reset_toolchain_cache()
    yield
    rs._reset_toolchain_cache()


def test_python3_only_host_resolves_python3(monkeypatch):
    # Only python3 on PATH (python absent) → commands must use python3.
    monkeypatch.setattr(rs.shutil, "which",
                        lambda c: "/usr/bin/python3" if c == "python3" else None)
    tc = rs.resolve_toolchain("python")
    assert tc["test_cmd"] == "python3 -m pytest -q"
    assert tc["compile_cmd"].startswith("python3 ")


def test_prefers_python3_when_both_present(monkeypatch):
    # Both on PATH → prefer python3 (python may be py2; python3 is safe).
    monkeypatch.setattr(rs.shutil, "which",
                        lambda c: f"/usr/bin/{c}" if c in ("python", "python3") else None)
    tc = rs.resolve_toolchain("python")
    assert tc["test_cmd"] == "python3 -m pytest -q"


def test_get_injects_resolved_interpreter(monkeypatch):
    monkeypatch.setattr(rs.shutil, "which",
                        lambda c: "/usr/bin/python3" if c == "python3" else None)
    std = rs.Standards(name="x", lang="python")
    rs._apply_defaults(std)
    assert std.test_cmd == "python3 -m pytest -q"


def test_probe_is_cached(monkeypatch):
    calls = {"n": 0}

    def _which(c):
        calls["n"] += 1
        return "/usr/bin/python3" if c == "python3" else None

    monkeypatch.setattr(rs.shutil, "which", _which)
    rs.resolve_toolchain("python")
    first = calls["n"]
    rs.resolve_toolchain("python")          # second call — cache hit
    assert calls["n"] == first              # no extra which() probes


def test_java_prefers_mvnw_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(rs.shutil, "which", lambda c: "/usr/bin/mvn")
    (tmp_path / "mvnw").write_text("#!/bin/sh\n")
    tc = rs.resolve_toolchain("java", str(tmp_path))
    assert tc["test_cmd"] == "./mvnw test"


def test_node_uses_yarn_when_lockfile(tmp_path, monkeypatch):
    monkeypatch.setattr(rs.shutil, "which", lambda c: "/usr/bin/npm")
    (tmp_path / "yarn.lock").write_text("")
    tc = rs.resolve_toolchain("node", str(tmp_path))
    assert tc["test_cmd"] == "yarn test"


def test_toolchain_brief_for_python_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(rs.shutil, "which",
                        lambda c: "/usr/bin/python3" if c == "python3" else None)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    brief = rs.toolchain_brief(str(tmp_path))
    assert "DETECTED TOOLCHAIN" in brief
    assert "python3 -m pytest -q" in brief
    assert "do NOT re-probe" in brief


def test_toolchain_brief_empty_when_no_lang(tmp_path):
    # bare dir, no markers → no fingerprint → empty (never a wrong guess)
    assert rs.toolchain_brief(str(tmp_path)) == ""
    assert rs.toolchain_brief(None) == ""
