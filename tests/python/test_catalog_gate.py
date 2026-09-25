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


# --- the native tool schemas follow the same gate --------------------------

def _names(schemas):
    return {s["function"]["name"] for s in schemas}


def test_schemas_everything_configured_is_a_noop():
    from aiforge_core.runtime.chat_agent._catalog_gate import gate_schemas
    from aiforge_core.runtime.chat_agent._tools._schemas import NATIVE_TOOL_SCHEMAS
    assert gate_schemas(NATIVE_TOOL_SCHEMAS, ALL) == NATIVE_TOOL_SCHEMAS


def test_schemas_of_unconfigured_integrations_are_not_sent():
    from aiforge_core.runtime.chat_agent._catalog_gate import gate_schemas
    from aiforge_core.runtime.chat_agent._tools._schemas import NATIVE_TOOL_SCHEMAS
    kept = _names(gate_schemas(NATIVE_TOOL_SCHEMAS, {"gitlab"}))
    assert not any(n.startswith(("jira_", "confluence_", "email_")) for n in kept)
    assert "gitlab_read" in kept and "file_read" in kept and "run_command" in kept
    assert "context_gather" not in kept, "shared Jira/Confluence tool, neither is set up"


def test_schemas_match_the_catalog_lines_the_prompt_keeps():
    """One rule for both: a tool is either advertised in both places or neither."""
    from aiforge_core.runtime.chat_agent._catalog_gate import gate_schemas
    from aiforge_core.runtime.chat_agent._tools._schemas import NATIVE_TOOL_SCHEMAS
    for have in (set(), {"jira"}, {"confluence", "email"}, ALL):
        out, _ = gate_catalog(_SYSTEM, have)
        kept = _names(gate_schemas(NATIVE_TOOL_SCHEMAS, have))
        for name in _names(NATIVE_TOOL_SCHEMAS):
            if f"- {name} " in _SYSTEM:
                assert (name in kept) == (f"- {name} " in out), (have, name)


def test_schemas_web_tools_follow_the_web_lockdown(monkeypatch):
    from aiforge_core.runtime.chat_agent import _catalog_gate
    from aiforge_core.runtime.chat_agent._tools._schemas import NATIVE_TOOL_SCHEMAS
    monkeypatch.setattr(_catalog_gate, "_web_fetch_on", lambda: False)
    kept = _names(_catalog_gate.gate_schemas(NATIVE_TOOL_SCHEMAS, ALL))
    assert "web_fetch" not in kept and "web_crawl" not in kept


def test_schemas_gate_off_keeps_the_integrations(monkeypatch):
    from aiforge_core.runtime.chat_agent._catalog_gate import gate_schemas
    from aiforge_core.runtime.chat_agent._tools._schemas import NATIVE_TOOL_SCHEMAS
    monkeypatch.setenv("AIFORGE_CHAT_GATE_TOOLS", "0")
    assert gate_schemas(NATIVE_TOOL_SCHEMAS, set()) == NATIVE_TOOL_SCHEMAS


def test_the_native_call_sends_the_gated_schemas(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime.chat_agent import _catalog_gate, _native
    monkeypatch.setattr(_catalog_gate, "configured_integrations", lambda: set())
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-gate")
    sent = []

    def _raw(role, convo, tools=None, tool_choice=None):
        sent.append(_names(tools))
        return {"role": "assistant", "content": "FINAL: ok"}
    monkeypatch.setattr(client, "complete_raw", _raw)
    fn = _native.make_native_complete_fn()
    fn("chat", [])
    fn("chat", [])
    assert sent[0] == sent[1]
    assert not any(n.startswith("jira_") for n in sent[0]) and "file_read" in sent[0]


def test_email_counts_as_configured_once_smtp_or_imap_has_a_host(monkeypatch):
    """The probe used to call a helper email_tool never had, so email was
    'not configured' on every install and its tools were hidden."""
    from aiforge_core.runtime.chat_agent._catalog_gate import configured_integrations
    for key in ("AIFORGE_SMTP_HOST", "AIFORGE_IMAP_HOST", "AIFORGE_EMAIL_DISABLE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("aiforge_core.runtime.tools.email_tool._stored", lambda: {})
    assert "email" not in configured_integrations()
    monkeypatch.setenv("AIFORGE_SMTP_HOST", "smtp.example.com")
    assert "email" in configured_integrations()
    monkeypatch.delenv("AIFORGE_SMTP_HOST")
    monkeypatch.setenv("AIFORGE_IMAP_HOST", "imap.example.com")
    assert "email" in configured_integrations()
    monkeypatch.setenv("AIFORGE_EMAIL_DISABLE", "1")
    assert "email" not in configured_integrations()


def test_the_gate_runs_once_per_turn(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime.chat_agent import _catalog_gate, _native
    probes = []
    monkeypatch.setattr(_catalog_gate, "configured_integrations",
                        lambda: probes.append(1) or set())
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-gate-once")
    monkeypatch.setattr(client, "complete_raw",
                        lambda *a, **k: {"role": "assistant", "content": "FINAL: ok"})
    fn = _native.make_native_complete_fn()
    for _ in range(3):
        fn("chat", [])
    assert len(probes) == 1


def test_a_test_written_this_turn_cannot_fail_correct_code():
    """A small model invents a wrong test, then treats the red result as a
    broken implementation and rewrites code that was already right."""
    assert "A test you wrote this turn does not count" in _SYSTEM
    assert "Never change the implementation to satisfy" in _SYSTEM
    assert "delete it" in _SYSTEM.lower()
