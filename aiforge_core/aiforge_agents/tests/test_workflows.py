"""Tests for the workflow registry + route detector + dispatcher."""
from __future__ import annotations

import pytest

# Importing the package triggers _builtin registration (trial-balance).
from aiforge_core import workflows as wf
from aiforge_core.workflows import (
    REGISTRY,
    TicketRoute,
    WorkflowSpec,
    detect_route,
    dispatch,
    get,
    list_all,
    register,
)
from aiforge_core.workflows.detector import preview, _score


@pytest.fixture
def isolated_registry():
    """Snapshot + restore REGISTRY so per-test mutations don't leak."""
    snapshot = dict(REGISTRY)
    yield REGISTRY
    REGISTRY.clear()
    REGISTRY.update(snapshot)


# ── registry basics ─────────────────────────────────────────────────


def test_builtin_trial_balance_registered():
    spec = get("tally-trial-balance")
    assert spec is not None
    assert spec.handler.endswith(":run_workflow")
    assert "tally" in spec.required_attachments
    assert "oneshell" in spec.required_attachments


def test_register_rejects_missing_id():
    with pytest.raises(ValueError, match="id is required"):
        register(WorkflowSpec(id="", label="X", handler="m:f"))


def test_register_rejects_missing_handler():
    with pytest.raises(ValueError, match="handler is required"):
        register(WorkflowSpec(id="x", label="X", handler=""))


def test_register_idempotent_on_id(isolated_registry):
    register(WorkflowSpec(id="dup", label="A", handler="m:f"))
    register(WorkflowSpec(id="dup", label="B", handler="m:g"))
    assert get("dup").label == "B"


def test_to_public_dict_omits_handler():
    spec = get("tally-trial-balance")
    pub = spec.to_public_dict()
    assert "handler" not in pub
    assert pub["id"] == "tally-trial-balance"
    assert "required_attachments" in pub


def test_list_all_returns_sorted():
    items = list_all()
    ids = [w.id for w in items]
    assert ids == sorted(ids)


# ── detector ────────────────────────────────────────────────────────


def test_detect_route_no_match_returns_code():
    r = detect_route(body="add a button to the login page")
    assert r.kind == "code"
    assert r.workflow_id is None
    assert r.source == "auto"


def test_detect_route_matches_trial_balance_via_keywords_only(isolated_registry):
    """Keywords alone don't reach 0.6 threshold — needs attachments too."""
    r = detect_route(body="please run trial balance reconciliation")
    # 0.5 (keyword) + 0.0 (no attachments) = 0.5 < 0.6 → code fallback
    assert r.kind == "code"


def test_detect_route_matches_with_keywords_and_attachments():
    r = detect_route(
        body="trial balance for acme corp",
        attachments=["tally", "oneshell"],
    )
    # 0.5 + 0.5 = 1.0 → match
    assert r.kind == "workflow"
    assert r.workflow_id == "tally-trial-balance"
    assert r.confidence >= 0.6
    assert "keyword" in r.rationale


def test_detect_route_attachments_alone_below_threshold(isolated_registry):
    """Attachments without keyword cue → 0.5 (still under default 0.6)."""
    r = detect_route(
        body="please process these files",
        attachments=["tally", "oneshell"],
    )
    assert r.kind == "code"


def test_detect_route_intent_action_boosts_score():
    r = detect_route(
        body="trial balance check",
        attachments=["tally", "oneshell"],
        intent={"action": "audit"},
    )
    # 0.5 + 0.5 + 0.2 = 1.2 → capped at 1.0
    assert r.confidence == 1.0


def test_score_caps_at_one(isolated_registry):
    spec = get("tally-trial-balance")
    score, _ = _score(spec, text="trial balance audit",
                      attachments={"tally", "oneshell"},
                      intent_action="audit")
    assert score <= 1.0


def test_preview_returns_chosen_and_candidates():
    out = preview(
        body="trial balance recon", title="ledger fix",
        attachments=["tally", "oneshell"],
    )
    assert out["chosen"]["kind"] == "workflow"
    assert out["chosen"]["workflow_id"] == "tally-trial-balance"
    assert any(
        c["workflow_id"] == "tally-trial-balance"
        for c in out["candidates"]
    )


def test_preview_returns_code_when_nothing_matches():
    out = preview(body="rename a function in the controller")
    assert out["chosen"]["kind"] == "code"
    assert out["candidates"] == []


# ── dispatcher ──────────────────────────────────────────────────────


def test_dispatch_unknown_id_raises_keyerror():
    with pytest.raises(KeyError, match="unknown workflow"):
        dispatch("does-not-exist", ticket={"id": "T1"})


def test_dispatch_invokes_handler(isolated_registry):
    """Register a stub workflow and confirm dispatch routes to it."""
    calls: list[dict] = []

    def fake_handler(ticket, *, log=None, **kw):
        calls.append({"ticket": ticket, "kw": kw})
        return {"artifact_type": "doer_outcome",
                "process": "stub", "applied": True}

    # Inject by registering a real spec pointing at this test module
    import sys
    this_mod = sys.modules[__name__]
    setattr(this_mod, "_fake_handler", fake_handler)
    register(WorkflowSpec(
        id="stub-test",
        label="stub",
        handler=f"{__name__}:_fake_handler",
    ))

    out = dispatch("stub-test", ticket={"id": "T1", "title": "x"})
    assert out["process"] == "stub"
    assert calls[0]["ticket"]["id"] == "T1"


def test_dispatch_handler_bad_path_raises_value_error(isolated_registry):
    register(WorkflowSpec(
        id="bad-handler",
        label="bad",
        handler="no_colon_here",
    ))
    with pytest.raises(ValueError, match="must be 'module:function'"):
        dispatch("bad-handler", ticket={"id": "T"})


def test_dispatch_handler_missing_function_raises_import_error(isolated_registry):
    register(WorkflowSpec(
        id="missing-fn",
        label="missing",
        handler="aiforge_core.workflows.detector:does_not_exist",
    ))
    with pytest.raises(ImportError, match="not found"):
        dispatch("missing-fn", ticket={"id": "T"})


# ── TicketRoute dataclass ──────────────────────────────────────────


def test_ticket_route_defaults():
    r = TicketRoute(kind="code")
    assert r.workflow_id is None
    assert r.confidence == 1.0
    assert r.source == "auto"
