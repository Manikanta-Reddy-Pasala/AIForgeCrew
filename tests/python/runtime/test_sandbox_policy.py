"""Gap A6: mandatory sandbox policy.

``AIFORGE_SANDBOX_REQUIRED=1`` forbids the silent host fallback — when
docker is unavailable the exec must refuse rather than run on host.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import docker_sandbox as ds


# --- sandbox_policy() reads env ---------------------------------------

def test_policy_off_when_nothing_set(monkeypatch):
    monkeypatch.delenv("AIFORGE_SANDBOX_REQUIRED", raising=False)
    monkeypatch.delenv("AIFORGE_DOCKER_SANDBOX", raising=False)
    assert ds.sandbox_policy() == "off"


def test_policy_preferred_when_docker_opt_in(monkeypatch):
    monkeypatch.delenv("AIFORGE_SANDBOX_REQUIRED", raising=False)
    monkeypatch.setenv("AIFORGE_DOCKER_SANDBOX", "1")
    assert ds.sandbox_policy() == "preferred"


def test_policy_required_overrides_preferred(monkeypatch):
    monkeypatch.setenv("AIFORGE_SANDBOX_REQUIRED", "1")
    monkeypatch.setenv("AIFORGE_DOCKER_SANDBOX", "1")
    assert ds.sandbox_policy() == "required"


def test_policy_required_without_docker_opt_in(monkeypatch):
    monkeypatch.setenv("AIFORGE_SANDBOX_REQUIRED", "1")
    monkeypatch.delenv("AIFORGE_DOCKER_SANDBOX", raising=False)
    assert ds.sandbox_policy() == "required"


def test_policy_accepts_true_string(monkeypatch):
    monkeypatch.delenv("AIFORGE_SANDBOX_REQUIRED", raising=False)
    monkeypatch.setenv("AIFORGE_DOCKER_SANDBOX", "true")
    assert ds.sandbox_policy() == "preferred"


# --- resolve_exec() full truth table ----------------------------------

@pytest.mark.parametrize(
    "policy,available,expected_mode",
    [
        ("required", True, "docker"),
        ("required", False, "refuse"),
        ("preferred", True, "docker"),
        ("preferred", False, "host"),
        ("off", True, "host"),
        ("off", False, "host"),
    ],
)
def test_resolve_exec_truth_table(
    monkeypatch, policy, available, expected_mode
):
    monkeypatch.setattr(ds, "sandbox_policy", lambda: policy)
    out = ds.resolve_exec(available)
    assert out["mode"] == expected_mode
    assert "reason" in out


def test_resolve_exec_refuse_carries_reason(monkeypatch):
    monkeypatch.setattr(ds, "sandbox_policy", lambda: "required")
    out = ds.resolve_exec(False)
    assert out["mode"] == "refuse"
    assert out["reason"]


# --- exec_in_container honours a "refuse" decision --------------------

def test_exec_refuses_when_required_and_docker_down(monkeypatch):
    monkeypatch.setattr(ds, "sandbox_policy", lambda: "required")
    monkeypatch.setattr(ds, "_docker_available", lambda: False)
    out = ds.exec_in_container("rid", "echo hi")
    assert out["ok"] is False
    assert out["error"] == "sandbox_required"
    # must NOT have run on host
    assert out.get("sandbox") != "host"


def test_is_enabled_true_when_required_even_if_docker_missing(monkeypatch):
    """``required`` must route into docker_sandbox so it can refuse,
    rather than letting bash.py silently fall through to host exec."""
    monkeypatch.setenv("AIFORGE_SANDBOX_REQUIRED", "1")
    monkeypatch.setattr(ds, "_docker_available", lambda: False)
    assert ds.is_enabled() is True
