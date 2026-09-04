"""The archetype-facing LLM shim: role inference and tolerant JSON.

`call_json` exists because local models wrap their answer in fences, add
prose, or emit four trailing backticks. Every one of those recoveries was
untested — the module scored 18% — so a regression in the fallback ladder
would have shown up as an archetype silently getting None.
"""
from __future__ import annotations

import pytest

from aiforge_core.orchestrator import llm_client as C

# ── fence stripping ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ('```json\n{"a": 1}\n```', '{"a": 1}'),
    ('```\n{"a": 1}\n```', '{"a": 1}'),
    ('\n\n```json\n{"a": 1}\n```\n\n', '{"a": 1}'),
    ('{"a": 1}', '{"a": 1}'),
    ('', ''),
])
def test_fences_are_stripped_as_whole_lines(raw, want):
    assert C._strip_fences(raw) == want


def test_a_fence_mid_answer_is_left_alone():
    """Only the first and last lines are fences; an inner one belongs to the
    content."""
    raw = '```\nline\n```\nmore\n```'
    assert "line" in C._strip_fences(raw)


# ── tolerant parsing ────────────────────────────────────────────────────────

def test_a_clean_object_parses():
    assert C._resilient_json_parse('{"ok": true}') == {"ok": True}


def test_a_fenced_object_parses():
    assert C._resilient_json_parse('```json\n{"ok": true}\n```') == {"ok": True}


def test_prose_around_the_object_is_ignored():
    raw = 'Here is the result:\n{"ok": true}\nHope that helps!'
    assert C._resilient_json_parse(raw) == {"ok": True}


def test_a_nested_fence_falls_through_to_the_first_to_last_slice():
    raw = 'prose ```json {"a": {"b": 1}} ``` trailing'
    assert C._resilient_json_parse(raw) == {"a": {"b": 1}}


@pytest.mark.parametrize("raw", [None, "", "   ", "not json at all",
                                 "[1, 2, 3]", "```\n```"])
def test_anything_that_is_not_an_object_is_None(raw):
    assert C._resilient_json_parse(raw) is None


def test_a_bare_list_is_not_accepted_as_a_dict():
    """The archetype contract is a dict; a list that happens to parse is not
    the answer it asked for."""
    assert C._loads_dict("[1,2]") is None


# ── role inference ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("hint,role", [
    (None, "doer"),
    ("Qwen3-Coder-Next", "doer"),
    ("some-coder-model", "doer"),
    ("gemma-27b", "planner"),
    ("qwen-32b", "planner"),
    ("planner-tuned", "planner"),
    ("mystery-model", "doer"),
])
def test_the_model_hint_maps_to_a_router_role(hint, role, monkeypatch):
    monkeypatch.delenv("AIFORGE_LLM_CLIENT_DEFAULT_ROLE", raising=False)
    assert C._role_for(hint) == role


def test_an_explicit_default_role_beats_every_hint(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_CLIENT_DEFAULT_ROLE", "verifier")
    assert C._role_for("Qwen3-Coder-Next") == "verifier"
    assert C._role_for(None) == "verifier"


# ── the two entry points ────────────────────────────────────────────────────

def test_call_text_routes_by_role_and_returns_the_content(monkeypatch):
    seen = {}

    def _fake(role, messages, **kw):
        seen["role"] = role
        seen["messages"] = messages
        seen["kw"] = kw
        return "the answer"

    monkeypatch.setattr(C, "_complete", _fake)
    out = C.call_text(system="sys", user="usr", role="planner",
                      temperature=0.3, max_tokens=99)
    assert out == "the answer"
    assert seen["role"] == "planner"
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    assert seen["kw"]["temperature"] == 0.3
    assert seen["kw"]["max_tokens"] == 99


def test_call_json_asks_for_a_json_object_and_parses_it(monkeypatch):
    seen = {}

    def _fake(role, messages, **kw):
        seen["extras"] = kw.get("extras")
        return '```json\n{"verdict": "pass"}\n```'

    monkeypatch.setattr(C, "_complete", _fake)
    assert C.call_json(system="s", user="u") == {"verdict": "pass"}
    assert seen["extras"] == {"response_format": {"type": "json_object"}}


def test_unparseable_output_is_retried_once_with_a_stricter_prompt(monkeypatch):
    calls = []

    def _fake(role, messages, **kw):
        calls.append(messages[0]["content"])
        return "sorry, no JSON" if len(calls) == 1 else '{"ok": 1}'

    monkeypatch.setattr(C, "_complete", _fake)
    assert C.call_json(system="base", user="u") == {"ok": 1}
    assert len(calls) == 2
    assert "SINGLE JSON object" in calls[1]
    assert calls[1].startswith("base")


def test_the_retry_can_be_switched_off(monkeypatch):
    calls = []

    def _fake(role, messages, **kw):
        calls.append(1)
        return "still not JSON"

    monkeypatch.setattr(C, "_complete", _fake)
    assert C.call_json(system="s", user="u", retry_on_invalid=False) is None
    assert len(calls) == 1


def test_two_failures_give_None_rather_than_a_half_answer(monkeypatch):
    monkeypatch.setattr(C, "_complete", lambda *a, **k: "prose only")
    assert C.call_json(system="s", user="u") is None
