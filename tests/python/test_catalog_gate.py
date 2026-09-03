"""Integration catalog gating — advertise only what this install can reach.

The chat system prompt lists 102 tools (~7.3k tokens) every turn. Twenty of
them are Jira. On a box with no Jira configured those lines teach the model
nothing except that twenty plausible tool names exist, which is exactly the
condition under which "get my tickets" gets answered by the issue creator.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.chat_agent._catalog_gate import gate_catalog
from aiforge_core.runtime.chat_agent._prompt import _SYSTEM

ALL = {"jira", "confluence", "gitlab", "email"}


@pytest.fixture(autouse=True)
def _web_on(monkeypatch):
    """These cases are about the INTEGRATION gate, so hold the (separate) web
    lockdown open — otherwise every assertion here also depends on whether the
    box running the suite happens to allow page fetching."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.delenv("AIFORGE_WEB_FETCH_DISABLE", raising=False)
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)


def test_everything_configured_is_a_noop():
    out, missing = gate_catalog(_SYSTEM, ALL)
    assert out == _SYSTEM
    assert missing == []


def test_unconfigured_families_are_dropped():
    out, missing = gate_catalog(_SYSTEM, set())
    assert missing == ["confluence", "email", "gitlab", "jira"]
    for prefix in ("- jira_", "- confluence_", "- gitlab_", "- email_"):
        assert prefix not in out
    assert len(out) < len(_SYSTEM)


def test_partial_config_keeps_only_that_family():
    out, _ = gate_catalog(_SYSTEM, {"jira"})
    assert "- jira_search" in out
    assert "- confluence_read" not in out
    assert "- gitlab_search" not in out


def test_shared_lines_survive_while_either_owner_is_configured():
    # context_gather / set_integration_default span Jira AND Confluence.
    with_jira, _ = gate_catalog(_SYSTEM, {"jira"})
    assert "- context_gather " in with_jira
    with_conf, _ = gate_catalog(_SYSTEM, {"confluence"})
    assert "- context_gather " in with_conf
    with_neither, _ = gate_catalog(_SYSTEM, {"gitlab"})
    assert "- context_gather " not in with_neither


def test_hidden_families_are_named_so_the_model_does_not_invent_them():
    out, _ = gate_catalog(_SYSTEM, set())
    assert "NOT CONFIGURED on this install" in out
    for fam in ALL:
        assert fam in out.rsplit("NOT CONFIGURED", 1)[1]


def test_general_tools_are_never_dropped():
    out, _ = gate_catalog(_SYSTEM, set())
    # web_fetch is only a "general tool" while the web switch is on (the
    # autouse fixture holds it open); its lockdown is covered below.
    for keeper in ("- memory_write", "- web_fetch", "- github_pr"):
        assert keeper in out


def test_gate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_GATE_TOOLS", "0")
    out, missing = gate_catalog(_SYSTEM, set())
    assert out == _SYSTEM
    assert missing == []


def test_a_failing_probe_counts_as_configured(monkeypatch):
    # Hiding a tool that actually works is worse than showing one that doesn't,
    # and a probe must never be what breaks a turn.
    from aiforge_core.runtime.chat_agent import _catalog_gate as cg

    def boom():
        raise RuntimeError("no network")

    monkeypatch.setitem(cg._PROBES, "jira", boom)
    assert "jira" in cg.configured_integrations()


# ── web lockdown ────────────────────────────────────────────────────────────
# Web SEARCH no longer exists; page FETCH is a switch. When the switch is off,
# advertising web_fetch/web_crawl teaches the model that a working tool exists
# — the same waste the integration gate was written to stop.

def test_web_lines_dropped_when_fetch_is_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "0")
    out, _ = gate_catalog(_SYSTEM, ALL)
    assert "- web_fetch " not in out
    assert "- web_crawl " not in out
    assert "WEB ACCESS IS OFF" in out


def test_web_lines_kept_when_fetch_is_on(monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.delenv("AIFORGE_WEB_FETCH_DISABLE", raising=False)
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    out, _ = gate_catalog(_SYSTEM, ALL)
    assert "- web_fetch " in out
    assert "WEB ACCESS IS OFF" not in out


def test_hard_off_switch_beats_the_allow_flag(monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.setenv("AIFORGE_WEB_FETCH_DISABLE", "1")
    out, _ = gate_catalog(_SYSTEM, ALL)
    assert "WEB ACCESS IS OFF" in out


def test_legacy_search_disable_var_still_locks_fetch(monkeypatch):
    """A box already locked down under AIFORGE_WEB_SEARCH_DISABLE must not
    reopen just because the search half of the module was deleted."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.delenv("AIFORGE_WEB_FETCH_DISABLE", raising=False)
    monkeypatch.setenv("AIFORGE_WEB_SEARCH_DISABLE", "1")
    out, _ = gate_catalog(_SYSTEM, ALL)
    assert "WEB ACCESS IS OFF" in out
