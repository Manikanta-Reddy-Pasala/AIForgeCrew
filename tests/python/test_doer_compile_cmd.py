"""Doer compile-cmd wiring — Standards → tools.run_compile + GA prompt.

These tests pin the bug fix for ONE-84: the Doer's pre-flight compile
gate, ``code_run`` tool, and system prompt all hardcoded
``mvn -DskipTests compile``. After the fix, every place flows through
``aiforge_core.runtime.repo_standards`` so Python / Node / Go / Java
repos each run their own catalogued command.
"""
from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_core.runtime import repo_standards as rs


# ─────────────────────────── make_run_compile argv ──────────────────────


class TestMakeRunCompileArgv:
    """Mock subprocess.run and assert the argv list matches the
    Standards-resolved compile_cmd. No live mvn / python / npm calls."""

    def test_python_repo_runs_compileall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ONE-84 repro: worktree YAML says python compile_cmd; tool
        must subprocess.run that exact argv split via shlex."""
        from aiforge_core.doer.tools import make_run_compile

        # Python sandbox: pyproject.toml + worktree YAML override.
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n", encoding="utf-8"
        )
        aiforge_dir = tmp_path / ".aiforge"
        aiforge_dir.mkdir()
        (aiforge_dir / "aiforge.conf.yml").write_text(
            "lang: python\ncompile_cmd: python -m compileall -q src\n",
            encoding="utf-8",
        )
        # Block Neo4j read; force YAML resolution path.
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)

        run_compile = make_run_compile(str(tmp_path))
        fake_proc = types.SimpleNamespace(
            returncode=0,
            stdout=b"",
            stderr=b"",
        )
        with patch(
            "aiforge_core.doer.tools.subprocess.run",
            return_value=fake_proc,
        ) as mock_run:
            output = run_compile()
        assert "EXIT=0" in output
        # Pull the argv positional arg from the call.
        called_argv = mock_run.call_args[0][0]
        assert called_argv == ["python", "-m", "compileall", "-q", "src"], (
            f"expected python compile_cmd argv, got {called_argv!r}"
        )

    def test_java_repo_still_runs_mvn_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No regression — Java repo without YAML still gets mvn."""
        from aiforge_core.doer.tools import make_run_compile

        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        # Make sure no global env override leaks in.
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        run_compile = make_run_compile(str(tmp_path))
        fake_proc = types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch(
            "aiforge_core.doer.tools.subprocess.run",
            return_value=fake_proc,
        ) as mock_run:
            run_compile()
        called_argv = mock_run.call_args[0][0]
        assert called_argv == ["mvn", "-q", "-DskipTests", "compile"]

    def test_node_repo_runs_tsc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiforge_core.doer.tools import make_run_compile

        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        run_compile = make_run_compile(str(tmp_path))
        fake_proc = types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch(
            "aiforge_core.doer.tools.subprocess.run",
            return_value=fake_proc,
        ) as mock_run:
            run_compile()
        called_argv = mock_run.call_args[0][0]
        assert called_argv == ["tsc", "--noEmit"]

    def test_go_repo_runs_go_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiforge_core.doer.tools import make_run_compile

        (tmp_path / "go.mod").write_text("module x", encoding="utf-8")
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        run_compile = make_run_compile(str(tmp_path))
        fake_proc = types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch(
            "aiforge_core.doer.tools.subprocess.run",
            return_value=fake_proc,
        ) as mock_run:
            run_compile()
        called_argv = mock_run.call_args[0][0]
        assert called_argv == ["go", "build", "./..."]

    def test_unknown_repo_skips_with_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC: defaults safety — when no compile_cmd resolves, the tool
        skips the gate (returns EXIT=0 + WARN) instead of running mvn."""
        from aiforge_core.doer.tools import make_run_compile

        # Empty tmp_path = no marker files; no YAML.
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        run_compile = make_run_compile(str(tmp_path))
        with patch(
            "aiforge_core.doer.tools.subprocess.run",
        ) as mock_run:
            output = run_compile()
        assert "EXIT=0" in output
        assert "no compile_cmd configured" in output
        # Critical: subprocess.run must NEVER be called when no cmd resolves.
        assert mock_run.call_count == 0

    def test_env_override_wins_over_standards(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AIFORGE_COMPILE_CMD env pin beats the resolved Standards
        manifest — operator pinning is preserved."""
        from aiforge_core.doer.tools import make_run_compile

        # Even with a Java tree, operator can pin python compile.
        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.setenv(
            "AIFORGE_COMPILE_CMD", "python -m py_compile main.py"
        )

        run_compile = make_run_compile(str(tmp_path))
        fake_proc = types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch(
            "aiforge_core.doer.tools.subprocess.run",
            return_value=fake_proc,
        ) as mock_run:
            run_compile()
        called_argv = mock_run.call_args[0][0]
        assert called_argv == ["python", "-m", "py_compile", "main.py"]


# ─────────────────────────── GA system prompt ───────────────────────────


class TestGaSystemPrompt:
    """The GA preamble string is a template; rendered at session-build
    time with the resolved compile_cmd. For a python worktree it must
    contain the python command and not the literal 'mvn'."""

    def test_python_repo_prompt_contains_python_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiforge_core.doer import ga_runner

        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        aiforge_dir = tmp_path / ".aiforge"
        aiforge_dir.mkdir()
        (aiforge_dir / "aiforge.conf.yml").write_text(
            "lang: python\ncompile_cmd: python -m compileall -q src\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        rendered = ga_runner._render_doer_preamble(str(tmp_path))
        assert "python -m compileall -q src" in rendered
        assert "mvn" not in rendered, (
            "rendered python prompt must not contain literal 'mvn'"
        )

    def test_java_repo_prompt_contains_mvn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiforge_core.doer import ga_runner

        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        rendered = ga_runner._render_doer_preamble(str(tmp_path))
        assert "mvn -q -DskipTests compile" in rendered

    def test_unknown_repo_prompt_marks_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No markers → the prompt advertises the gate as skipped, not
        a blind 'mvn' command."""
        from aiforge_core.doer import ga_runner

        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        rendered = ga_runner._render_doer_preamble(str(tmp_path))
        assert "no compile_cmd configured" in rendered
        # Acceptable: no mvn token in the executable instructions.
        assert "mvn -DskipTests compile" not in rendered
        assert "mvn -q -DskipTests compile" not in rendered
