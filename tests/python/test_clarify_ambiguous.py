"""clarify.py's pre-pipeline gate surfaces ambiguous rule matches as extra
signal for the LLM clarity check — self-contained, computes its own
ambiguity check so it works BEFORE the pipeline's own rules collection."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime import clarify as cl


def _ticket(interactive=True, clarified=False, metadata=None):
    md = dict(metadata or {})
    md["interactive"] = interactive
    md["clarified"] = clarified
    return SimpleNamespace(id=1, identifier="T-1", title="Deploy",
                           body="deploy release now", metadata=md)


def test_ambiguous_candidates_empty_on_no_rules(monkeypatch):
    monkeypatch.setattr(
        "aiforge_core.runtime.repo_rules.collect_or_ask",
        lambda *a, **k: ("", []))
    assert cl._ambiguous_candidates(_ticket()) == []


def test_ambiguous_candidates_reports_names(monkeypatch):
    from aiforge_core.runtime.repo_rules import Rule
    r1 = Rule(name="deploy-staging", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    r2 = Rule(name="deploy-prod", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    monkeypatch.setattr(
        "aiforge_core.runtime.repo_rules.collect_or_ask",
        lambda *a, **k: ("rendered", [[r1, r2]]))
    names = cl._ambiguous_candidates(_ticket())
    assert names == ["'deploy-staging' or 'deploy-prod'"]


def test_ask_llm_includes_ambiguous_note(monkeypatch):
    seen = {}

    def fake_complete(role, convo, **kw):
        seen["user"] = convo[-1]["content"]
        return "CLEAR"

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)
    cl._ask_llm(_ticket(), ambiguous=["'deploy-staging' or 'deploy-prod'"])
    assert "deploy-staging" in seen["user"]
    assert "near-equal confidence" in seen["user"]


def test_maybe_clarify_still_skips_autonomous_tickets(monkeypatch):
    def must_not_run(*a, **k):
        raise AssertionError("clarify LLM must not run for autonomous tickets")

    monkeypatch.setattr(cl, "_ask_llm", must_not_run)
    assert cl.maybe_clarify(_ticket(interactive=False)) is False
