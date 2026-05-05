"""Unit tests for ``aiforge_core.config.repo_standards``.

Covers:
  * ``detect_lang`` for java / node / go / python / empty fallback
  * ``get`` honours auto-detect when neither Neo4j nor YAML supplies lang
  * Per-language defaults populate compile_cmd correctly
  * Operator-set ``lang`` is NOT overridden by auto-detect

All tests are offline — no Neo4j, no LLM, no real shell calls.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_core.runtime import repo_standards as rs


# ─────────────────────────── detect_lang ────────────────────────────────


class TestDetectLang:
    def test_pom_xml_means_java(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        assert rs.detect_lang(str(tmp_path)) == "java"

    def test_build_gradle_means_java(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").write_text("// gradle", encoding="utf-8")
        assert rs.detect_lang(str(tmp_path)) == "java"

    def test_build_gradle_kts_means_java(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle.kts").write_text("// gradle kts", encoding="utf-8")
        assert rs.detect_lang(str(tmp_path)) == "java"

    def test_package_json_means_node(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert rs.detect_lang(str(tmp_path)) == "node"

    def test_go_mod_means_go(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module x", encoding="utf-8")
        assert rs.detect_lang(str(tmp_path)) == "go"

    def test_pyproject_means_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n",
                                                 encoding="utf-8")
        assert rs.detect_lang(str(tmp_path)) == "python"

    def test_requirements_txt_means_python(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        assert rs.detect_lang(str(tmp_path)) == "python"

    def test_empty_dir_returns_empty_string(self, tmp_path: Path) -> None:
        # No marker files present.
        assert rs.detect_lang(str(tmp_path)) == ""

    def test_nonexistent_path_returns_empty_string(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert rs.detect_lang(str(missing)) == ""

    def test_empty_string_path_returns_empty_string(self) -> None:
        assert rs.detect_lang("") == ""

    def test_priority_pom_over_package_json(self, tmp_path: Path) -> None:
        # When both exist (rare polyglot tree), Java wins. This matches the
        # documented priority ordering.
        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert rs.detect_lang(str(tmp_path)) == "java"


# ─────────────────────────── get(...) auto-detect path ─────────────────


class TestGetAutoDetect:
    def test_python_repo_default_compile_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC: with only pyproject.toml and no Neo4j/YAML lang, get() returns
        the python defaults — including the stricter compileall command."""
        # Stub the Neo4j read so we don't depend on a running DB.
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='x'\n", encoding="utf-8"
        )
        std = rs.get("sandbox", worktree=str(tmp_path))
        assert std.lang == "python"
        assert std.compile_cmd == "python -m compileall -q ."
        assert std.test_cmd == "python -m pytest -q"
        assert std.source == "auto-detect"

    def test_node_repo_default_compile_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        std = rs.get("nodebox", worktree=str(tmp_path))
        assert std.lang == "node"
        assert std.compile_cmd == "tsc --noEmit"
        assert std.build_cmd == "npm run build"

    def test_go_repo_default_compile_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        (tmp_path / "go.mod").write_text("module x", encoding="utf-8")
        std = rs.get("gobox", worktree=str(tmp_path))
        assert std.lang == "go"
        assert std.compile_cmd == "go build ./..."
        assert std.test_cmd == "go test ./..."

    def test_java_repo_default_compile_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC: Java repo without YAML still gets mvn — no regression."""
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        std = rs.get("javabox", worktree=str(tmp_path))
        assert std.lang == "java"
        assert std.compile_cmd == "mvn -q -DskipTests compile"

    def test_unknown_repo_returns_empty_compile_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC: defaults safety — no markers, no fallback to mvn."""
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        std = rs.get("mystery", worktree=str(tmp_path))
        assert std.lang == ""
        assert std.compile_cmd == ""

    def test_explicit_lang_not_overridden_by_auto_detect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC: operator-pinned lang via worktree YAML wins over marker files.

        Tree has both pom.xml (would auto-detect java) AND a YAML that
        says lang=python — the YAML must win.
        """
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        aiforge_dir = tmp_path / ".aiforge"
        aiforge_dir.mkdir()
        (aiforge_dir / "aiforge.conf.yml").write_text(
            "lang: python\ncompile_cmd: python -m compileall -q src\n",
            encoding="utf-8",
        )
        std = rs.get("polyglot", worktree=str(tmp_path))
        assert std.lang == "python"
        assert std.compile_cmd == "python -m compileall -q src"

    def test_yaml_compile_cmd_wins_over_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ONE-84 repro: Python sandbox repo + YAML compile_cmd."""
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        aiforge_dir = tmp_path / ".aiforge"
        aiforge_dir.mkdir()
        (aiforge_dir / "aiforge.conf.yml").write_text(
            "lang: python\ncompile_cmd: python -m compileall -q src\n",
            encoding="utf-8",
        )
        std = rs.get("one84-repro", worktree=str(tmp_path))
        assert std.compile_cmd == "python -m compileall -q src"
