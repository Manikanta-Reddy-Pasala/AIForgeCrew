from __future__ import annotations

from pathlib import Path

import pytest

from hermes.tools import build_default_registry
from aiforge_core.permissions import PermissionDenied

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sr_developer_can_read_docs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    reg = build_default_registry(REPO_ROOT, "sr-developer")
    # docs/** is in sr-developer's read allow list per security/file-access-rules.yml.
    r = reg.dispatch("sr-developer", "read_file", {"path": "docs/hardware-guide.md"})
    assert len(r["content"]) > 0


def test_tester_cannot_write_src() -> None:
    reg = build_default_registry(REPO_ROOT, "tester")
    with pytest.raises(PermissionDenied):
        reg.dispatch("tester", "write_file", {"path": "src/evil.py", "content": "pass"})


def test_sr_architect_cannot_run_tests() -> None:
    # sr-architect lacks hermes_execute per DESIGN §5.2 — tool not visible.
    reg = build_default_registry(REPO_ROOT, "sr-architect")
    tools = [t.name for t in reg.list_for_role("sr-architect")]
    assert "run_tests" not in tools


def test_tester_has_run_tests() -> None:
    reg = build_default_registry(REPO_ROOT, "tester")
    tools = [t.name for t in reg.list_for_role("tester")]
    assert "run_tests" in tools


def test_blocked_path_denied_even_if_role_has_write() -> None:
    # secrets/** is in globally_blocked; even if a (buggy) rule said allow, blocked wins.
    reg = build_default_registry(REPO_ROOT, "sr-developer")
    with pytest.raises(PermissionDenied):
        reg.dispatch("sr-developer", "write_file", {"path": "secrets/leak.txt", "content": "x"})


def test_path_escape_rejected(tmp_path: Path) -> None:
    reg = build_default_registry(REPO_ROOT, "sr-developer")
    with pytest.raises((PermissionDenied, FileNotFoundError)):
        reg.dispatch("sr-developer", "read_file", {"path": "../../etc/passwd"})
