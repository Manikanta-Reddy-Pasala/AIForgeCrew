"""Unit tests for the 12 coding-standards gap modules."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from aiforge_core.runtime import (
    ci_feedback,
    diff_impact,
    pr_reviewer,
    scope_guard,
    spec_to_tests,
)
from aiforge_core.runtime.tools import format as format_tool
from aiforge_core.runtime.tools import test_runner, typecheck


# C6 — scope guard ---------------------------------------------------------

def test_scope_matches_simple_glob() -> None:
    assert scope_guard._matches_any("src/foo.py", ["src/**"])
    assert not scope_guard._matches_any("docs/x.md", ["src/**"])
    assert scope_guard._matches_any("./src/foo.py", ["src/**"])


def test_scope_path_extract_editor() -> None:
    paths = scope_guard._path_from_args(
        "editor", {"command": "create", "path": "src/foo.py"},
    )
    assert paths == ["src/foo.py"]


def test_scope_view_command_returns_no_paths() -> None:
    # ``view`` is read-only — never blocked even if path outside scope.
    paths = scope_guard._path_from_args(
        "editor", {"command": "view", "path": "secrets/keys.txt"},
    )
    assert paths == []


# C1 — CI feedback ---------------------------------------------------------

def test_pr_url_parse() -> None:
    parsed = ci_feedback._parse_pr_url(
        "https://github.com/Foo/Bar/pull/123",
    )
    assert parsed == ("Foo", "Bar", "123")


def test_pr_url_parse_bad() -> None:
    assert ci_feedback._parse_pr_url("https://example.com") is None
    assert ci_feedback._parse_pr_url("") is None


# C2 — Reviewer ------------------------------------------------------------

def test_reviewer_parse_pr_url() -> None:
    assert pr_reviewer._parse_pr_url(
        "https://github.com/Acme/repo/pull/9",
    ) == ("Acme", "repo", "9")


# C5 — Spec to tests -------------------------------------------------------

def test_extract_acceptance_finds_bullets() -> None:
    body = (
        "Some intro.\n\n"
        "## Acceptance\n"
        "- README has the badge\n"
        "- Line count is exactly 12\n\n"
        "Out of scope: anything else.\n"
    )
    bullets = spec_to_tests._extract_acceptance(body)
    assert "README has the badge" in bullets
    assert "Line count is exactly 12" in bullets


def test_extract_acceptance_empty_without_header() -> None:
    assert spec_to_tests._extract_acceptance("- random bullet") == []


def test_write_scaffold_python(tmp_path) -> None:
    body = (
        "## Acceptance\n"
        "- adds X\n"
        "- never edits Y\n"
    )
    out = spec_to_tests.write_scaffold(
        "ONE-99", body, repo_root=str(tmp_path), language="python",
    )
    assert out["ok"]
    assert out["preexisting"] is False
    p = tmp_path / "tests" / "aiforge_spec" / "test_one_99.py"
    assert p.is_file()
    content = p.read_text(encoding="utf-8")
    assert "test_01_adds_x" in content
    assert "test_02_never_edits_y" in content
    assert "pytest.skip" in content


def test_write_scaffold_idempotent(tmp_path) -> None:
    body = "## Acceptance\n- only one\n"
    first = spec_to_tests.write_scaffold(
        "ONE-1", body, repo_root=str(tmp_path), language="python",
    )
    second = spec_to_tests.write_scaffold(
        "ONE-1", body, repo_root=str(tmp_path), language="python",
    )
    assert first["preexisting"] is False
    assert second["preexisting"] is True
    assert first["path"] == second["path"]


# C12 — Diff impact --------------------------------------------------------

def test_diff_impact_empty_inputs_returns_empty() -> None:
    assert diff_impact.impacted_tests("", [], driver=None) == []
    assert diff_impact.impacted_tests("repo", [], driver=None) == []


# Tools: format / typecheck / test_runner ---------------------------------

def test_format_unsupported_suffix(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    out = format_tool.format("x.unknownext")
    assert out["ok"] is False
    assert out["error"] == "unsupported"


def test_format_empty_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    out = format_tool.format("")
    assert out["ok"] is False
    assert out["error"] == "empty_path"


def test_typecheck_no_language(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    out = typecheck.typecheck()
    assert out["ok"] is False
    assert out["error"] == "no_language"


def test_test_runner_no_language(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    out = test_runner.run_tests()
    assert out["ok"] is False
    assert out["error"] == "no_language"


def test_test_runner_finds_a_pytest_ini_project_and_runs_it(tmp_path, monkeypatch) -> None:
    """A project marked only by pytest.ini (no pyproject/setup.py) is Python;
    with no pytest script on PATH the module runs through python3."""
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_a.py").write_text("def test_a():\n    assert True\n")
    real_which = test_runner.shutil.which
    monkeypatch.setattr(test_runner.shutil, "which",
                        lambda name, *a, **k: None if name == "pytest" else real_which(name, *a, **k))
    out = test_runner.run_tests(mode="all")
    assert out.get("language") == "python", out
    assert out["ok"] is True, out


def test_a_node_repo_with_a_helper_requirements_txt_stays_node(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "requirements.txt").write_text("requests\n")
    (tmp_path / "setup.cfg").write_text("")
    assert test_runner._pick_runner() == ("node", "npm")


def test_no_pytest_anywhere_is_a_missing_tool(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    monkeypatch.setattr(test_runner.shutil, "which", lambda name, *a, **k: None)
    out = test_runner.run_tests()
    assert out["ok"] is False and out["error"] == "missing_tool"


def test_a_pytest_suite_with_frontend_tooling_stays_python(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "conftest.py").write_text("")
    assert test_runner._pick_runner() == ("python", "pytest")
