"""Unit tests for the runner's project-exclusion list (Tally tickets
are handled out-of-band, never auto-claimed). Pure env parsing — no DB."""
from __future__ import annotations

import pytest

from aiforge_core.tickets import store


def test_default_excludes_tally(monkeypatch) -> None:
    monkeypatch.delenv("AIFORGE_RUNNER_EXCLUDE_PROJECTS", raising=False)
    excl = store._excluded_projects()
    assert "TallyConnector" in excl
    assert "Tally Connector" in excl


def test_env_override_replaces_default(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_RUNNER_EXCLUDE_PROJECTS", "FooRepo,BarRepo")
    excl = store._excluded_projects()
    assert excl == ["FooRepo", "BarRepo"]
    assert "TallyConnector" not in excl


def test_env_trims_whitespace_and_blanks(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_RUNNER_EXCLUDE_PROJECTS", " A , ,B ,")
    assert store._excluded_projects() == ["A", "B"]


def test_empty_env_excludes_nothing(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_RUNNER_EXCLUDE_PROJECTS", "")
    assert store._excluded_projects() == []
