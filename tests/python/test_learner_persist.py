"""Unit tests for runtime.learner_persist — Learner facts → Neo4j."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.runtime import learner_persist as lp


@pytest.fixture(autouse=True)
def _reset_semantic_env(monkeypatch):
    """Clear the AIFORGE_SEMANTIC_DEDUPE* env vars before each test so
    NUC-side state from an earlier test (or from runtime.env) doesn't
    leak into the no-env baseline cases."""
    for var in (
        "AIFORGE_SEMANTIC_DEDUPE",
        "AIFORGE_SEMANTIC_DEDUPE_HARD",
        "AIFORGE_SEMANTIC_DEDUPE_SOFT",
        "AIFORGE_LEARNER_PERSIST_DISABLE",
    ):
        monkeypatch.delenv(var, raising=False)
    # Force semantic dedupe OFF so the legacy routing/empty-text/error
    # paths never reach the embed-sidecar path under test.
    monkeypatch.setenv("AIFORGE_SEMANTIC_DEDUPE", "0")
    # These tests exercise the Neo4j/AFM write path; pin a Neo4j URI so
    # backend_select doesn't route to the (now-default) embedded SQLite
    # backend. The embedded path has its own tests in
    # test_memory_write_routing.py.
    monkeypatch.setenv("NEO4J_URI", "bolt://test:7687")


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


def _build_semantic_fake_module(
    *, recall_score: float, embed_dim: int = 1024,
):
    """Build a MagicMock-backed AFM store stub that returns a single
    ``recall_observations`` hit with the requested score."""
    fake_obs = MagicMock(return_value={"id": "o", "label": "Observation_v2"})
    fake_dec = MagicMock(return_value={"id": "d", "label": "Decision_v2"})
    fake_recall = MagicMock(return_value=[
        {"id": "existing-obs-1", "text": "older paraphrase",
         "kind": "learning", "tags": [], "score": recall_score},
    ])
    return fake_obs, fake_dec, fake_recall, MagicMock(
        upsert_observation=fake_obs,
        upsert_decision=fake_dec,
        recall_observations=fake_recall,
    )


def test_persist_facts_semantic_hard_dupe_skips_write(monkeypatch) -> None:
    """sim ≥ HARD threshold → skip the upsert and count as
    ``skipped_semantic_dupes``."""
    monkeypatch.setenv("AIFORGE_SEMANTIC_DEDUPE", "1")
    monkeypatch.setenv("AIFORGE_SEMANTIC_DEDUPE_HARD", "0.95")
    monkeypatch.setattr(lp, "_open_driver", lambda: MagicMock())
    import aiforge_core.memory.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "embed", lambda text: [0.1] * 1024)
    fake_obs, _, fake_recall, fake_module = _build_semantic_fake_module(
        recall_score=0.97,
    )
    with patch.dict(
        "sys.modules",
        {"aiforge_memory.features.memory.store": fake_module},
    ):
        out = lp.persist_facts(
            facts=[{"text": "paraphrase of an existing fact"}],
            repo="MyRepo",
        )
    assert out["written_observations"] == 0
    assert out.get("skipped_semantic_dupes") == 1
    fake_obs.assert_not_called()
    fake_recall.assert_called_once()


def test_persist_facts_semantic_soft_dupe_tags_supersede(monkeypatch) -> None:
    """SOFT ≤ sim < HARD → write the new fact AND tag it with
    ``superseded-check:<existing_id>`` so a compactor can reconcile."""
    monkeypatch.setenv("AIFORGE_SEMANTIC_DEDUPE", "1")
    monkeypatch.setenv("AIFORGE_SEMANTIC_DEDUPE_HARD", "0.95")
    monkeypatch.setenv("AIFORGE_SEMANTIC_DEDUPE_SOFT", "0.85")
    monkeypatch.setattr(lp, "_open_driver", lambda: MagicMock())
    import aiforge_core.memory.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "embed", lambda text: [0.1] * 1024)
    fake_obs, _, _, fake_module = _build_semantic_fake_module(
        recall_score=0.9,
    )
    with patch.dict(
        "sys.modules",
        {"aiforge_memory.features.memory.store": fake_module},
    ):
        out = lp.persist_facts(
            facts=[{"text": "close paraphrase"}],
            repo="MyRepo",
        )
    assert out["written_observations"] == 1
    fake_obs.assert_called_once()
    tags = fake_obs.call_args.kwargs["tags"]
    assert any(t.startswith("superseded-check:") for t in tags)
    # Gap #2: the new fact actively supersedes the stale near-dup so the
    # old one drops out of recall (not just an audit tag).
    assert fake_obs.call_args.kwargs.get("supersedes") == ["existing-obs-1"]


def test_persist_facts_semantic_below_soft_writes_clean(monkeypatch) -> None:
    """sim < SOFT → write normally, no supersede tag."""
    monkeypatch.setenv("AIFORGE_SEMANTIC_DEDUPE", "1")
    monkeypatch.setattr(lp, "_open_driver", lambda: MagicMock())
    import aiforge_core.memory.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "embed", lambda text: [0.1] * 1024)
    fake_obs, _, _, fake_module = _build_semantic_fake_module(
        recall_score=0.5,
    )
    with patch.dict(
        "sys.modules",
        {"aiforge_memory.features.memory.store": fake_module},
    ):
        out = lp.persist_facts(facts=[{"text": "fresh fact"}], repo="MyRepo")
    assert out["written_observations"] == 1
    tags = fake_obs.call_args.kwargs["tags"]
    assert not any(t.startswith("superseded-check:") for t in tags)
    # Gap #2: nothing to supersede below the soft threshold.
    assert not fake_obs.call_args.kwargs.get("supersedes")


def test_persist_facts_semantic_disabled_via_env(monkeypatch) -> None:
    """``AIFORGE_SEMANTIC_DEDUPE=0`` skips both embed + recall calls;
    behaviour reverts to pre-v2 (write every fact, AFM does its own
    exact-text dedupe)."""
    monkeypatch.setenv("AIFORGE_SEMANTIC_DEDUPE", "0")
    monkeypatch.setattr(lp, "_open_driver", lambda: MagicMock())
    bad_embed = MagicMock(side_effect=RuntimeError("must not be called"))
    monkeypatch.setattr("aiforge_core.memory.embed.embed", bad_embed)
    fake_obs, _, fake_recall, fake_module = _build_semantic_fake_module(
        recall_score=0.97,
    )
    with patch.dict(
        "sys.modules",
        {"aiforge_memory.features.memory.store": fake_module},
    ):
        out = lp.persist_facts(facts=[{"text": "any text"}], repo="MyRepo")
    assert out["written_observations"] == 1
    bad_embed.assert_not_called()
    fake_recall.assert_not_called()
    # embed_vec stays None when semantic dedupe disabled
    assert fake_obs.call_args.kwargs["embed_vec"] is None


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
