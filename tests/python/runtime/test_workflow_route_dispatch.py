"""A ticket routed to a named workflow runs that workflow's handler.

The route was stored, shown and validated, but nothing ever called
workflows.dispatch: a trial-balance ticket went through the LLM code cascade
in a repo worktree instead.
"""
import pytest

from aiforge_core.runtime.adk_runner import _orchestrate as orch


def _ticket(**kw):
    base = dict(id=7, identifier="T-7", title="trial balance", body="", status="in_progress",
                priority="medium", assignee_role=None, parent_id=None, branch=None,
                project=None, labels=[], metadata={}, created_at=None, updated_at=None,
                completed_at=None, route="workflow", route_workflow="wf-x",
                route_source="manual", route_confidence=1.0)
    base.update(kw)
    from aiforge_core.tickets.store import Ticket
    return Ticket(**base)


@pytest.fixture
def calls(monkeypatch):
    got = {"status": [], "comments": [], "dispatch": []}
    monkeypatch.setattr(orch.tickets_mod, "update_status",
                        lambda tid, st, **k: got["status"].append((tid, st, k)))
    monkeypatch.setattr(orch.tickets_mod, "add_comment",
                        lambda tid, role, body, meta=None: got["comments"].append((role, body)))
    monkeypatch.setattr(orch, "_setup_ticket_workspace",
                        lambda t: pytest.fail("a workflow ticket must not build a worktree"))
    monkeypatch.setattr(orch, "_clarify_parked",
                        lambda t: pytest.fail("a workflow ticket is not clarified"))
    return got


def _handler(outcome, got):
    def dispatch(wf, ticket, log=None):
        got["dispatch"].append((wf, ticket["identifier"]))
        return outcome
    return dispatch


def test_a_clean_workflow_run_completes_with_its_report(monkeypatch, calls):
    from aiforge_core import workflows
    monkeypatch.setattr(workflows, "dispatch", _handler(
        {"udiff": "# Report\nall matched", "problems": [],
         "blocked_by_detectors": False, "target": "r.md"}, calls))
    orch._run_claimed_ticket(_ticket())
    assert calls["dispatch"] == [("wf-x", "T-7")]
    assert calls["comments"] == [("workflow", "# Report\nall matched")]
    tid, status, kw = calls["status"][-1]
    assert status == "done"
    assert kw["metadata_patch"]["workflow"] == "wf-x"


def test_material_gaps_block_with_the_reason(monkeypatch, calls):
    from aiforge_core import workflows
    monkeypatch.setattr(workflows, "dispatch", _handler(
        {"udiff": "", "blocked_by_detectors": True,
         "problems": [{"mode": "missing_attachment", "evidence": "tally"}]}, calls))
    orch._run_claimed_ticket(_ticket())
    _tid, status, kw = calls["status"][-1]
    assert status == "blocked"
    assert "missing_attachment: tally" in kw["metadata_patch"]["blocked_reason"]


def test_an_unknown_workflow_blocks_instead_of_running_the_code_pipeline(monkeypatch, calls):
    # No worktree was made, so the partial-work rescue (commit + push + PR of
    # whatever repo it resolves) must not run for a workflow ticket.
    monkeypatch.setattr(orch, "_rescue_partial_work",
                        lambda t: pytest.fail("rescued a workflow ticket's 'work'"))
    monkeypatch.setattr(orch, "_log_run_failure", lambda t, e: None)
    orch._run_claimed_ticket(_ticket(route_workflow="no-such-workflow"))
    _tid, status, kw = calls["status"][-1]
    assert status == "blocked"
    assert "no-such-workflow" in kw["metadata_patch"]["error"]


def test_code_tickets_still_take_the_pipeline(monkeypatch):
    seen = []
    monkeypatch.setattr(orch, "_clarify_parked", lambda t: seen.append("clarify") or True)
    orch._run_claimed_ticket(_ticket(route="code", route_workflow=None))
    assert seen == ["clarify"]


def test_the_run_repo_root_is_per_run(monkeypatch):
    """Team chat sets the repo per run in the request context; the ADK
    pipeline's rules/preferences read that, not a process-global env var."""
    from aiforge_core.runtime import request_context
    from aiforge_core.runtime.adk_runner import _pipeline
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/global")
    tok = request_context.set_repo_root("/this-run")
    try:
        assert _pipeline._run_repo_root() == "/this-run"
    finally:
        request_context.reset_repo_root(tok)
    assert _pipeline._run_repo_root() == "/global"
