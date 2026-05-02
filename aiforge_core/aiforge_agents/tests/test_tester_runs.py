"""Cover Tester-runs-tests (#2): framework detection + tail parsing."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aiforge_core.aiforge_agents.orchestrator import run_ticket as rt


# ─────────── Framework detection ───────────────────────────────────────

def test_detect_maven_with_wrapper(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "mvnw").write_text("#!/bin/sh")
    (tmp_path / "mvnw").chmod(0o755)
    fw, argv = rt._detect_test_framework(str(tmp_path))
    assert fw == "maven"
    assert argv[0] == "./mvnw"
    assert "test" in argv


def test_detect_maven_without_wrapper(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    fw, argv = rt._detect_test_framework(str(tmp_path))
    assert fw == "maven"
    assert argv[0] == "mvn"


def test_detect_gradle(tmp_path):
    (tmp_path / "build.gradle").write_text("// build")
    fw, argv = rt._detect_test_framework(str(tmp_path))
    assert fw == "gradle"
    assert "test" in argv


def test_detect_gradle_kotlin_dsl(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("// kotlin dsl")
    fw, argv = rt._detect_test_framework(str(tmp_path))
    assert fw == "gradle"


def test_detect_npm(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x"}')
    fw, argv = rt._detect_test_framework(str(tmp_path))
    assert fw == "npm"
    assert argv[0] == "npm"


def test_detect_pytest_via_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    fw, argv = rt._detect_test_framework(str(tmp_path))
    assert fw == "pytest"
    assert argv[0] == "pytest"


def test_detect_pytest_via_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    fw, argv = rt._detect_test_framework(str(tmp_path))
    assert fw == "pytest"


def test_detect_returns_none_when_unknown(tmp_path):
    (tmp_path / "README.md").write_text("hi")
    assert rt._detect_test_framework(str(tmp_path)) is None


def test_detect_returns_none_when_path_missing():
    assert rt._detect_test_framework("/nope/does/not/exist") is None
    assert rt._detect_test_framework("") is None


def test_maven_priority_over_npm(tmp_path):
    """Java repos with package.json (tooling) still detect as maven."""
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "package.json").write_text('{"name":"tooling"}')
    fw, _ = rt._detect_test_framework(str(tmp_path))
    assert fw == "maven"


# ─────────── Output summary parser ─────────────────────────────────────

def test_summarise_pytest():
    stdout = "===== 12 passed, 1 failed in 3.45s ====="
    out = rt._summarise_test_run("pytest", stdout, "", 1)
    assert out["passed"] == 12
    assert out["failed"] == 1
    assert out["ok"] is False


def test_summarise_pytest_all_passed():
    stdout = "===== 5 passed in 0.10s ====="
    out = rt._summarise_test_run("pytest", stdout, "", 0)
    assert out["passed"] == 5
    assert out["failed"] == 0
    assert out["ok"] is True


def test_summarise_maven_passed():
    stdout = "Tests run: 42, Failures: 0, Errors: 0, Skipped: 1"
    out = rt._summarise_test_run("maven", stdout, "", 0)
    assert out["passed"] == 42
    assert out["failed"] == 0


def test_summarise_maven_with_failures():
    stdout = "Tests run: 10, Failures: 2, Errors: 1, Skipped: 0"
    out = rt._summarise_test_run("maven", stdout, "", 1)
    assert out["passed"] == 7   # 10 - (2 + 1)
    assert out["failed"] == 3
    assert out["ok"] is False


def test_summarise_npm_jest_format():
    stdout = "Tests:       12 passed, 1 failed, 13 total"
    out = rt._summarise_test_run("npm", stdout, "", 1)
    assert out["passed"] == 12
    assert out["failed"] == 1


def test_summarise_unknown_framework_returns_zeros():
    out = rt._summarise_test_run("nope", "lol", "", 1)
    assert out["passed"] == 0
    assert out["failed"] == 0


# ─────────── Run wrapper ───────────────────────────────────────────────

def test_run_repo_tests_no_framework_returns_empty(tmp_path, monkeypatch):
    class _Log:
        def info(self, *a, **kw):
            pass

    out = rt._run_repo_tests(repo_path=str(tmp_path),
                             ticket_id="T", log=_Log(), timeout_s=5)
    assert out == {}


def test_run_repo_tests_invokes_subprocess(tmp_path, monkeypatch):
    """When framework detected, subprocess.run is called with cwd=repo."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    captured: dict = {}

    def fake_run(argv, cwd, capture_output, text, timeout):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(
            args=argv, returncode=0,
            stdout="===== 3 passed in 0.01s =====", stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    class _Log:
        def info(self, *a, **kw):
            pass

    out = rt._run_repo_tests(repo_path=str(tmp_path),
                             ticket_id="T", log=_Log(), timeout_s=5)
    assert out["framework"] == "pytest"
    assert out["passed"] == 3
    assert out["ok"] is True
    assert captured["cwd"] == str(tmp_path)


def test_run_repo_tests_handles_timeout(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    def fake_run(argv, cwd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout,
                                        output=b"partial",
                                        stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    class _Log:
        def info(self, *a, **kw):
            pass

    out = rt._run_repo_tests(repo_path=str(tmp_path),
                             ticket_id="T", log=_Log(), timeout_s=1)
    assert out["timed_out"] is True
    assert out["ok"] is False


def test_run_repo_tests_handles_spawn_failure(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    def fake_run(argv, cwd, capture_output, text, timeout):
        raise FileNotFoundError("no pytest in PATH")

    monkeypatch.setattr(subprocess, "run", fake_run)

    class _Log:
        def info(self, *a, **kw):
            pass

    out = rt._run_repo_tests(repo_path=str(tmp_path),
                             ticket_id="T", log=_Log(), timeout_s=5)
    assert out["ok"] is False
    assert out["exit_code"] == -2
    assert "no pytest" in out["error"]
