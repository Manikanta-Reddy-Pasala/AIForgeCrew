"""Unit tests for the runner's project-exclusion list (tickets of an
excluded project are handled out-of-band, never auto-claimed). Pure env
parsing — no DB."""
from __future__ import annotations

import pytest

from aiforge_core.tickets import store


def test_default_excludes_nothing(monkeypatch) -> None:
    monkeypatch.delenv("AIFORGE_RUNNER_EXCLUDE_PROJECTS", raising=False)
    assert store._excluded_projects() == []


def test_env_lists_excluded_projects(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_RUNNER_EXCLUDE_PROJECTS",
                       "BillingConnector,Billing Connector")
    excl = store._excluded_projects()
    assert "BillingConnector" in excl
    assert "Billing Connector" in excl


def test_ticket_prefix_defaults_and_overrides(monkeypatch) -> None:
    monkeypatch.delenv("AIFORGE_TICKET_PREFIX", raising=False)
    assert store.ticket_prefix() == "AF"
    monkeypatch.setenv("AIFORGE_TICKET_PREFIX", "JIRA")
    assert store.ticket_prefix() == "JIRA"


def test_env_override_replaces_default(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_RUNNER_EXCLUDE_PROJECTS", "FooRepo,BarRepo")
    excl = store._excluded_projects()
    assert excl == ["FooRepo", "BarRepo"]
    assert "BillingConnector" not in excl


def test_env_trims_whitespace_and_blanks(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_RUNNER_EXCLUDE_PROJECTS", " A , ,B ,")
    assert store._excluded_projects() == ["A", "B"]


def test_empty_env_excludes_nothing(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_RUNNER_EXCLUDE_PROJECTS", "")
    assert store._excluded_projects() == []
