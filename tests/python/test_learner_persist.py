"""Unit tests for runtime.learner_persist — Learner facts → SQLite memory."""
from __future__ import annotations

from aiforge_core.runtime import learner_persist as lp


def test_coerce_facts_handles_python_list() -> None:
    facts = [{"text": "x", "tags": ["a"]}, {"text": "y"}]
    assert lp._coerce_facts(facts) == facts


def test_coerce_facts_parses_json_string() -> None:
    raw = '[{"text": "hello", "tags": []}]'
    out = lp._coerce_facts(raw)
    assert out == [{"text": "hello", "tags": []}]


def test_coerce_facts_handles_none() -> None:
    assert lp._coerce_facts(None) == []


def test_coerce_facts_drops_non_dicts() -> None:
    assert lp._coerce_facts(["string", 123, {"text": "ok"}]) == [{"text": "ok"}]


def test_coerce_facts_invalid_json_returns_empty() -> None:
    assert lp._coerce_facts("not json") == []


def test_persist_facts_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_LEARNER_PERSIST_DISABLE", "1")
    out = lp.persist_facts(facts=[{"text": "x"}], repo="r")
    assert out["written_observations"] == 0
    assert "disabled_via_env" in out["errors"]


def test_persist_facts_no_facts_returns_zero() -> None:
    out = lp.persist_facts(facts=[], repo="r")
    assert out == {"written_observations": 0, "written_decisions": 0, "errors": []}


def test_persist_facts_no_repo_returns_zero() -> None:
    out = lp.persist_facts(facts=[{"text": "x"}], repo="")
    assert out["written_observations"] == 0


def test_persist_facts_routes_to_sqlite(monkeypatch) -> None:
    """persist_facts persists via the embedded SQLite path — the only
    backend. Decision-prefixed text is a decision, plain text a learning."""
    captured: list = []
    monkeypatch.setattr(
        lp, "_persist_facts_embedded",
        lambda **kw: (captured.append(kw)
                      or {"written_observations": 1, "written_decisions": 1,
                          "errors": []}))
    # md-mirror + OKR authoring are best-effort side effects; keep them quiet.
    monkeypatch.setattr(lp, "_mirror_facts_to_md", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_author_okr_solutions", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_update_repo_card", lambda *a, **k: None)
    out = lp.persist_facts(
        facts=[{"text": "Use ruff for lint"},
               {"text": "DECISION: drop smolagents"}],
        repo="MyRepo", ticket_identifier="ONE-1")
    assert out["written_observations"] == 1
    assert out["written_decisions"] == 1
    assert captured
    assert captured[0]["repo"] == "MyRepo"
