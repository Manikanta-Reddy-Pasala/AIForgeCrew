"""Unit tests for runtime.learner_persist — Learner facts → Neo4j."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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


def test_persist_facts_routes_decision_prefix(monkeypatch) -> None:
    """`DECISION:`-prefixed text writes Decision_v2, plain text writes
    Observation_v2."""
    fake_obs = MagicMock(return_value={"id": "o1", "label": "Observation_v2"})
    fake_dec = MagicMock(return_value={"id": "d1", "label": "Decision_v2"})

    fake_driver = MagicMock()
    monkeypatch.setattr(lp, "_open_driver", lambda: fake_driver)
    fake_module = MagicMock(
        upsert_observation=fake_obs,
        upsert_decision=fake_dec,
    )
    with patch.dict(
        "sys.modules",
        {"aiforge_memory.features.memory.store": fake_module},
    ):
        out = lp.persist_facts(
            facts=[
                {"text": "Use ruff for lint", "tags": ["style"]},
                {"text": "DECISION: drop smolagents", "tags": ["arch"]},
            ],
            repo="MyRepo",
            ticket_identifier="ONE-1",
        )
    assert out["written_observations"] == 1
    assert out["written_decisions"] == 1
    assert out["errors"] == []
    fake_obs.assert_called_once()
    fake_dec.assert_called_once()
    # Ticket id appears in tags
    obs_tags = fake_obs.call_args.kwargs["tags"]
    assert "ticket:ONE-1" in obs_tags


def test_persist_facts_skips_empty_text(monkeypatch) -> None:
    fake_obs = MagicMock(return_value={"id": "o", "label": "Observation_v2"})
    fake_driver = MagicMock()
    monkeypatch.setattr(lp, "_open_driver", lambda: fake_driver)
    fake_module = MagicMock(upsert_observation=fake_obs,
                            upsert_decision=MagicMock())
    with patch.dict(
        "sys.modules",
        {"aiforge_memory.features.memory.store": fake_module},
    ):
        out = lp.persist_facts(
            facts=[{"text": ""}, {"text": "  "}, {"text": "real"}],
            repo="r",
        )
    assert out["written_observations"] == 1


def test_persist_facts_handles_neo4j_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(lp, "_open_driver", lambda: None)
    out = lp.persist_facts(facts=[{"text": "x"}], repo="r")
    assert out["written_observations"] == 0
    assert "neo4j_unreachable" in out["errors"]


def test_persist_facts_soft_fails_on_upsert_error(monkeypatch) -> None:
    fake_driver = MagicMock()
    monkeypatch.setattr(lp, "_open_driver", lambda: fake_driver)

    def _bang(*a, **kw):
        raise RuntimeError("cypher boom")

    fake_module = MagicMock(
        upsert_observation=_bang, upsert_decision=MagicMock(),
    )
    with patch.dict(
        "sys.modules",
        {"aiforge_memory.features.memory.store": fake_module},
    ):
        out = lp.persist_facts(facts=[{"text": "x"}], repo="r")
    assert out["written_observations"] == 0
    assert any("upsert_failed" in e for e in out["errors"])
