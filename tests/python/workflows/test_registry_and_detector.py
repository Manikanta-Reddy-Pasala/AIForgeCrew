"""Which workflow a ticket routes to, and what the registry refuses.

The detector decides whether a ticket goes to the code path or to a named
workflow, from a score built out of keyword / attachment / intent triggers.
Every threshold in it was untested, so a spec with a typo'd trigger table
would simply never fire and nothing would say so.
"""
from __future__ import annotations

import pytest

from aiforge_core.workflows import detector as D
from aiforge_core.workflows import registry as R


@pytest.fixture
def empty_registry(monkeypatch):
    """A registry of our own — the real one is process-global."""
    reg: dict[str, R.WorkflowSpec] = {}
    monkeypatch.setattr(R, "REGISTRY", reg)
    monkeypatch.setattr(D, "REGISTRY", reg)
    return reg


def _spec(wid="tb", **triggers):
    return R.WorkflowSpec(
        id=wid, label=wid.upper(), description="d",
        handler="aiforge_core.workflows.registry:get",
        triggers=triggers,
    )


# ── the registry ────────────────────────────────────────────────────────────

def test_a_spec_without_an_id_is_refused(empty_registry):
    spec = R.WorkflowSpec(id="", label="x", handler="m:f")
    with pytest.raises(ValueError, match="id is required"):
        R.register(spec)


def test_a_spec_without_a_handler_is_refused(empty_registry):
    """A registered workflow that cannot be dispatched is worse than an
    unregistered one: the UI offers it and the run fails later."""
    spec = R.WorkflowSpec(id="x", label="x")
    with pytest.raises(ValueError, match="handler is required"):
        R.register(spec)


def test_registering_the_same_id_replaces_rather_than_duplicates(empty_registry):
    R.register(_spec("tb"))
    R.register(_spec("tb"))
    assert list(empty_registry) == ["tb"]


def test_list_all_is_sorted_by_id(empty_registry):
    for wid in ("zeta", "alpha", "mid"):
        R.register(_spec(wid))
    assert [s.id for s in R.list_all()] == ["alpha", "mid", "zeta"]


def test_get_of_an_unknown_id_is_None(empty_registry):
    assert R.get("nope") is None


def test_the_public_view_hides_the_handler(empty_registry):
    """The UI dropdown must not carry import paths."""
    spec = _spec("tb", keywords_any=["x"])
    pub = spec.to_public_dict()
    assert "handler" not in pub
    assert pub["id"] == "tb"
    assert pub["triggers"] == {"keywords_any": ["x"]}


def test_the_public_view_copies_its_collections(empty_registry):
    """Registry is process-global; a caller mutating what it got back must
    not poison the next reader."""
    spec = _spec("tb", keywords_any=["x"])
    pub = spec.to_public_dict()
    pub["triggers"]["keywords_any"].append("poison")
    pub["tags"].append("poison")
    assert spec.triggers["keywords_any"] == ["x"]
    assert spec.tags == []


# ── handler resolution ──────────────────────────────────────────────────────

def test_a_handler_path_without_a_colon_is_refused():
    with pytest.raises(ValueError, match="module:function"):
        R._resolve_handler("aiforge_core.workflows.registry")


def test_a_missing_function_names_both_halves():
    with pytest.raises(ImportError, match="no_such_function"):
        R._resolve_handler("aiforge_core.workflows.registry:no_such_function")


def test_a_non_callable_attribute_is_refused():
    with pytest.raises(TypeError, match="not callable"):
        R._resolve_handler("aiforge_core.workflows.registry:REGISTRY")


def test_dispatch_calls_the_handler_with_the_ticket(empty_registry, monkeypatch):
    seen = {}

    def _handler(ticket, log=None, **kw):
        seen["ticket"] = ticket
        seen["log"] = log
        seen["kw"] = kw
        return {"applied": True}

    monkeypatch.setattr(R, "_resolve_handler", lambda p: _handler)
    R.register(_spec("tb"))
    out = R.dispatch("tb", {"identifier": "ONE-1"}, log="LOG", extra=1)
    assert out == {"applied": True}
    assert seen["ticket"] == {"identifier": "ONE-1"}
    assert seen["log"] == "LOG"
    assert seen["kw"] == {"extra": 1}


def test_dispatching_an_unknown_id_raises_KeyError(empty_registry):
    with pytest.raises(KeyError, match="unknown workflow id"):
        R.dispatch("nope", {})


# ── scoring ─────────────────────────────────────────────────────────────────

def test_nothing_registered_means_the_code_path(empty_registry):
    route = D.detect_route(body="anything at all")
    assert route.kind == "code"
    assert route.workflow_id is None
    assert route.rationale == "no workflow trigger matched"


def test_a_single_keyword_scores_below_the_default_threshold(empty_registry):
    """0.5 for keywords_any, and the default floor is 0.6 — a lone keyword is
    a hint, not a routing decision."""
    R.register(_spec("tb", keywords_any=["trial balance"]))
    assert D.detect_route(body="please check the Trial Balance").kind == "code"


def test_a_keyword_plus_the_required_attachments_routes(empty_registry):
    R.register(_spec("tb", keywords_any=["trial balance"],
                     attachments_all=["tally", "oneshell"]))
    route = D.detect_route(body="trial balance please",
                           attachments=["tally", "oneshell"])
    assert route.kind == "workflow"
    assert route.workflow_id == "tb"
    assert route.confidence == 1.0          # 0.5 + 0.5, capped
    assert "keyword:trial balance" in route.rationale
    assert "attachments_all:tally,oneshell" in route.rationale


def test_a_spec_can_lower_its_own_threshold(empty_registry):
    R.register(_spec("tb", keywords_any=["trial balance"],
                     min_confidence=0.4))
    assert D.detect_route(body="trial balance").workflow_id == "tb"


def test_keywords_all_needs_every_word(empty_registry):
    R.register(_spec("tb", keywords_all=["stock", "reconcile"],
                     min_confidence=0.3))
    assert D.detect_route(body="reconcile the stock").workflow_id == "tb"
    assert D.detect_route(body="reconcile the ledger").kind == "code"


def test_attachments_any_is_weaker_than_attachments_all(empty_registry):
    R.register(_spec("a", attachments_any=["tally"], min_confidence=0.1))
    R.register(_spec("b", attachments_all=["tally"], min_confidence=0.1))
    route = D.detect_route(body="x", attachments=["tally"])
    assert route.workflow_id == "b"          # 0.5 beats 0.3


def test_the_intent_action_adds_a_little(empty_registry):
    R.register(_spec("tb", keywords_any=["trial balance"],
                     intent_action_in=["reconcile"]))
    route = D.detect_route(body="trial balance",
                           intent={"action": "reconcile"})
    assert route.workflow_id == "tb"
    assert route.confidence == 0.7           # 0.5 + 0.2
    assert "intent_action:reconcile" in route.rationale


def test_an_intent_with_no_action_is_simply_ignored(empty_registry):
    R.register(_spec("tb", keywords_any=["trial balance"],
                     intent_action_in=["reconcile"], min_confidence=0.4))
    assert D.detect_route(body="trial balance", intent={}).confidence == 0.5


def test_the_highest_scorer_wins(empty_registry):
    R.register(_spec("weak", keywords_any=["stock"], min_confidence=0.1))
    R.register(_spec("strong", keywords_any=["stock"],
                     attachments_all=["sheet"], min_confidence=0.1))
    route = D.detect_route(body="stock", attachments=["sheet"])
    assert route.workflow_id == "strong"


def test_the_title_counts_as_much_as_the_body(empty_registry):
    R.register(_spec("tb", keywords_any=["trial balance"], min_confidence=0.4))
    assert D.detect_route(title="Trial Balance", body="").workflow_id == "tb"


# ── preview ─────────────────────────────────────────────────────────────────

def test_preview_shows_the_near_misses_the_route_dropped(empty_registry):
    """The UI needs the alternatives, including the one that scored but did
    not clear its threshold — otherwise 'why did this go to code?' has no
    answer on screen."""
    R.register(_spec("near", keywords_any=["stock"]))          # 0.5 < 0.6
    R.register(_spec("hit", keywords_any=["stock"],
                     attachments_all=["sheet"]))               # 1.0
    out = D.preview("stock please", attachments=["sheet"])
    assert out["chosen"]["workflow_id"] == "hit"
    ids = [c["workflow_id"] for c in out["candidates"]]
    assert ids == ["hit", "near"]                              # sorted by score
    near = out["candidates"][1]
    assert near["above_threshold"] is False
    assert near["threshold"] == 0.6


def test_preview_with_no_candidates_still_reports_the_code_route(empty_registry):
    out = D.preview("nothing matches here")
    assert out["candidates"] == []
    assert out["chosen"]["kind"] == "code"
