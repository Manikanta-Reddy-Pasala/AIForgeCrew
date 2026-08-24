"""Integration catalog gating — advertise only what this install can reach.

The chat system prompt lists 102 tools (~7.3k tokens) every turn. Twenty of
them are Jira. On a box with no Jira configured those lines teach the model
nothing except that twenty plausible tool names exist, which is exactly the
condition under which "get my tickets" gets answered by the issue creator.
"""
from __future__ import annotations

from aiforge_core.runtime.chat_agent._catalog_gate import gate_catalog
from aiforge_core.runtime.chat_agent._prompt import _SYSTEM

ALL = {"jira", "confluence", "gitlab", "email"}


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
    for keeper in ("- memory_write", "- web_search", "- github_pr"):
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
