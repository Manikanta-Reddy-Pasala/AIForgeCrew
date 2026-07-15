"""LLM task-type classifier: word→category mapping, safe fallback to None on
disable/error/ambiguity, and the deterministic trivial→chat short-circuit."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import task_router as tr


def _stub_llm(monkeypatch, answer):
    def fake_complete(role, messages, **kw):
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)


@pytest.mark.parametrize("word,cat", [
    ("TRACKER", "tracker"),
    ("BUILD", "code_build"),
    ("DOC", "doc_analysis"),
    ("EDIT", "code_edit"),
    ("CHAT", "chat"),
])
def test_word_maps_to_category(monkeypatch, word, cat):
    monkeypatch.delenv("AIFORGE_LLM_TASK_ROUTER", raising=False)
    _stub_llm(monkeypatch, word)
    assert tr.classify_task("create 2 jira tickets about the API") == cat


def test_tolerates_extra_tokens(monkeypatch):
    monkeypatch.delenv("AIFORGE_LLM_TASK_ROUTER", raising=False)
    _stub_llm(monkeypatch, "Answer: TRACKER.")
    assert tr.classify_task("file a story for the billing api") == "tracker"


def test_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_TASK_ROUTER", "0")
    _stub_llm(monkeypatch, "BUILD")
    assert tr.classify_task("build an app") is None      # → caller uses regex


def test_llm_error_returns_none(monkeypatch):
    monkeypatch.delenv("AIFORGE_LLM_TASK_ROUTER", raising=False)
    _stub_llm(monkeypatch, RuntimeError("model down"))
    assert tr.classify_task("build an app") is None


def test_ambiguous_answer_returns_none(monkeypatch):
    monkeypatch.delenv("AIFORGE_LLM_TASK_ROUTER", raising=False)
    _stub_llm(monkeypatch, "hmm not sure")
    assert tr.classify_task("do the thing") is None


def test_trivial_prompt_short_circuits_to_chat(monkeypatch):
    monkeypatch.delenv("AIFORGE_LLM_TASK_ROUTER", raising=False)

    def boom(*a, **k):
        raise AssertionError("no LLM call for a greeting")
    monkeypatch.setattr("aiforge_core.llm.client.complete", boom)
    assert tr.classify_task("hi") == "chat"


def test_empty_prompt_returns_none():
    assert tr.classify_task("") is None
