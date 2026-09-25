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


def test_jira_and_confluence_default_to_a_crisp_flow():
    # The writing rule is an operating principle, so it stays even when the
    # tool catalog is gated down to nothing. The write-tool schemas repeat it
    # next to the fields the model fills in.
    from aiforge_core.runtime.chat_agent._tools._schemas import CATALOG

    assert "write crisp by default" in _SYSTEM
    assert "When EDITING an existing" in _SYSTEM
    assert "post it as given" in _SYSTEM
    out, _ = gate_catalog(_SYSTEM, set())
    assert "write crisp by default" in out
    by_name = {name: desc for name, (desc, _props, _req) in CATALOG.items()}
    for name in ("jira_create", "confluence_create",
                 "jira_comment", "confluence_comment"):
        assert "unless they asked for more" in by_name[name]
    assert "short numbered flow" in by_name["jira_create"]
    assert "Do not invent a flowchart" in by_name["confluence_create"]


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


def test_the_system_prompt_does_not_turn_the_tool_list_into_the_whole_catalog():
    """The prompt names Jira, Confluence, GitLab, email and URLs. Counting
    those as the user asking for them sent the gated catalog (~63 tools).
    The banner and the native call share one list, built from the user's
    words."""
    from aiforge_core.runtime.chat_agent import _native
    from aiforge_core.runtime.chat_agent._prompt import _SYSTEM
    convo = [
        {"role": "system", "content": _SYSTEM.format(cwd="/tmp")},
        {"role": "user", "content":
            "You are AIForge, an autonomous coding assistant.\n"
            "- jira_search\n- confluence_read\nhttps://example.com email gitlab"},
        {"role": "user", "content":
            "what is the test cycle?\n\n---\n[Interpreted request — a "
            "restatement]\nCheck jira, confluence, gitlab, email and "
            "https://example.com"},
    ]
    names = _names(_native.select_native_tools(convo, mode="act"))
    assert "file_read" in names and "file_patch" in names
    assert not any(n.startswith(("jira_", "confluence_", "gitlab_",
                                 "email_", "web_")) for n in names)
    assert len(names) < 25


def test_the_native_call_sends_the_same_list_the_banner_counts(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime.chat_agent import _native
    from aiforge_core.runtime.chat_agent._prompt import _SYSTEM
    convo = [
        {"role": "system", "content": _SYSTEM.format(cwd="/tmp")},
        {"role": "user", "content": "what is the test cycle?"},
    ]
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-short")
    sent = []

    def _raw(role, messages, tools=None, tool_choice=None):
        sent.append(_names(tools or []))
        return {"role": "assistant", "content": "FINAL: ok"}
    monkeypatch.setattr(client, "complete_raw", _raw)
    _native.make_native_complete_fn()("chat", convo)
    banner = _names(_native.select_native_tools(convo, mode="act"))
    assert sent[0] == banner
    assert len(sent[0]) < 25
    assert "file_read" in sent[0]
    assert not any(n.startswith("jira_") for n in sent[0])


def test_act_mode_offers_the_core_tools_not_every_integration():
    from aiforge_core.runtime.chat_agent._tools._schemas import (
        NATIVE_TOOL_SCHEMAS, filter_native)
    names = _names(filter_native(NATIVE_TOOL_SCHEMAS, mode="act",
                                 text="what is 2+2"))
    assert "file_read" in names and "file_patch" in names
    assert "run_command" in names
    assert not any(n.startswith("jira_") for n in names)
    assert len(names) < 25


def test_naming_jira_adds_it_and_plan_mode_stays_read_only():
    from aiforge_core.runtime.chat_agent._tools._schemas import (
        NATIVE_TOOL_SCHEMAS, filter_native)
    named = _names(filter_native(NATIVE_TOOL_SCHEMAS, mode="act",
                                 text="show my jira tickets"))
    assert any(n.startswith("jira_") for n in named)
    plan = _names(filter_native(NATIVE_TOOL_SCHEMAS, mode="plan",
                                text="plan the jira change"))
    assert "file_read" in plan and "jira_search" in plan
    assert "file_write" not in plan and "run_command" not in plan
    assert "jira_create" not in plan


def test_a_read_file_does_not_add_tools_but_a_steer_does():
    from aiforge_core.runtime.chat_agent import _native
    from aiforge_core.runtime.chat_agent._tools._schemas import (
        NATIVE_TOOL_SCHEMAS, filter_native)
    text = _native._convo_text([
        {"role": "user", "content": "fix the typo"},
        {"role": "user", "content":
            'OBSERVATION: {"text": "see https://example.com and a ticket"}'},
        {"role": "user", "content":
            "OBSERVATION: {}\n\n[NEW MESSAGE FROM THE USER]\ncheck jira"},
        {"role": "user", "content":
            "[automated verification — not the user] tests failed.\n\n"
            "TEST OUTPUT:\n-- Docs: https://docs.pytest.org/en/stable/\n"
            "tests/ticket/test_x.py"},
        {"role": "user", "content":
            "[automated syntax check — not the user] syntax error:\n"
            'requests.get("https://api.x.com")'},
    ])
    assert "https://" not in text and "ticket" not in text and "jira" in text
    names = _names(filter_native(NATIVE_TOOL_SCHEMAS, mode="act", text=text))
    assert not any(n.startswith("web_") for n in names)
    assert any(n.startswith("jira_") for n in names)
    rejected = _native._convo_text([{
        "role": "user",
        "content": (
            "OBSERVATION: wrote 0 bytes\n\n"
            "The user rejected the last action and gave this correction: "
            "check jira first\n"
            "Adjust accordingly and CONTINUE the current task."
        ),
    }])
    assert "jira" in rejected
    rejected_names = _names(filter_native(
        NATIVE_TOOL_SCHEMAS, mode="act", text=rejected))
    assert any(n.startswith("jira_") for n in rejected_names)


def test_a_job_builder_is_offered_its_finalize_tool():
    from aiforge_core.runtime.chat_agent._tools._schemas import (
        NATIVE_TOOL_SCHEMAS, filter_native)
    names = _names(filter_native(
        NATIVE_TOOL_SCHEMAS, mode="act", text="a nightly job", builder="job"))
    assert "create_job_script" in names


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
