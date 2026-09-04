"""The ticket runner: seed prompt, verdict gates, PR, and the poll loop.

The gates after the run are the substance. Two of them are ground truth git
can establish and a model's prose cannot: a diff of only test files is a
demotion, and a "pass" with a CLEAN TREE is a false pass — the Doer narrated an
edit it never wrote, which is exactly how ONE-163/164 both landed "done" with
no commit. A third distinguishes finished-but-imperfect work: partial WITH a
PR goes to in_review so a human looks at it, partial without one stays blocked
because there is nothing to review.

Around that: the Enhancer's "too vague" sentinel blocks before the Planner
burns a run on garbage, and a ticket that crashes the run still gets its
partial work rescued as a PR rather than dropped.
"""
from __future__ import annotations

import os
import types as pytypes

import pytest

from aiforge_core.runtime.adk_runner import _orchestrate as orc


def _ticket(**kw):
    base = {"id": 1, "identifier": "ONE-1", "title": "Fix the parser",
            "body": "details", "project": "app", "metadata": {}}
    base.update(kw)
    return pytypes.SimpleNamespace(**base)


# ─── the seed prompt ───────────────────────────────────────────────────


def test_operator_comments_are_folded_in_chronologically(monkeypatch):
    """The Enhancer only reads ticket.body — without this the follow-up is
    invisible to the whole run."""
    monkeypatch.setattr(orc.tickets_mod, "comments", lambda tid: [
        {"kind": "comment", "agent_role": "human", "body": "also fix the CLI",
         "created_at": "2026-01-01T10:00:00Z"},
        {"kind": "comment", "agent_role": "doer", "body": "working on it"},
        {"kind": "stage_done", "agent_role": "human", "body": "ignored"},
    ])
    out = orc._operator_comments_block(_ticket())
    assert "also fix the CLI" in out
    assert "working on it" not in out            # the agent's own commentary
    assert "authoritative extensions" in out


def test_no_human_comments_adds_nothing(monkeypatch):
    monkeypatch.setattr(orc.tickets_mod, "comments", lambda tid: [])
    assert orc._operator_comments_block(_ticket()) == ""


def test_an_unreadable_comment_store_adds_nothing(monkeypatch):
    monkeypatch.setattr(orc.tickets_mod, "comments",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("db down")))
    assert orc._operator_comments_block(_ticket()) == ""


def test_attachments_are_listed_by_their_worktree_path():
    t = _ticket(metadata={"attached_files": [
        {"path": ".aiforge/ticket-files/ONE-1/spec.pdf", "name": "spec.pdf",
         "size": 1200},
        "not a dict"]})
    out = orc._attachments_block(t)
    assert ".aiforge/ticket-files/ONE-1/spec.pdf" in out
    assert "1200 bytes" in out
    assert "file_read" in out


def test_a_ticket_with_no_attachments_says_nothing():
    assert orc._attachments_block(_ticket()) == ""


def test_skills_and_workflows_are_searched_for_the_ticket(monkeypatch):
    from aiforge_core.runtime import skills, workflows
    monkeypatch.setattr(skills, "auto_context", lambda hay, cwd: "SKILL BLOCK")
    monkeypatch.setattr(skills, "selected_names", lambda hay, cwd: ["deploy"])
    monkeypatch.setattr(workflows, "auto_context", lambda hay, cwd: "WF BLOCK")
    monkeypatch.setattr(workflows, "selected_names", lambda hay, cwd: ["release"])
    prefix, sk, wf = orc._playbook_prefix("fix the parser", "/repo")
    assert prefix.startswith("WF BLOCK")
    assert "SKILL BLOCK" in prefix
    assert sk == ["deploy"]
    assert wf == ["release"]


def test_a_broken_registry_does_not_stop_the_prompt(monkeypatch):
    from aiforge_core.runtime import skills, workflows
    monkeypatch.setattr(skills, "auto_context",
                        lambda hay, cwd: (_ for _ in ()).throw(RuntimeError("bad")))
    monkeypatch.setattr(workflows, "auto_context", lambda hay, cwd: "")
    assert orc._playbook_prefix("hay", None) == ("", [], [])


def test_the_injected_playbooks_are_recorded(monkeypatch):
    from aiforge_core.runtime import observability as obs
    seen: dict = {}
    monkeypatch.setattr(obs, "emit_context_injected", lambda **kw: seen.update(kw))
    orc._emit_context_injected(_ticket(), ["deploy"], [])
    assert seen == {"ticket_id": 1, "agent_role": "pipeline",
                    "skills": ["deploy"], "workflows": []}


def test_nothing_injected_emits_nothing(monkeypatch):
    from aiforge_core.runtime import observability as obs
    monkeypatch.setattr(obs, "emit_context_injected",
                        lambda **kw: pytest.fail("emitted with nothing injected"))
    orc._emit_context_injected(_ticket(), [], [])


@pytest.fixture
def vision(monkeypatch):
    import aiforge_core.config.agent_config as ac
    import aiforge_core.runtime.vision as v
    monkeypatch.setattr(ac, "load_all", lambda: {"doer": {"model": "vlm"}})
    monkeypatch.setattr(v, "supports_vision", lambda model: True)


def test_images_are_flagged_for_a_vision_doer(vision):
    t = _ticket(metadata={"attached_files": [
        {"name": "shot.PNG", "path": "/a.png"}, {"name": "spec.pdf", "path": "/b"}]})
    out = orc._vision_block(t)
    assert "vision-enabled model" in out
    assert "/a.png" in out
    assert "/b" not in out


def test_a_text_only_doer_gets_no_vision_block(monkeypatch):
    import aiforge_core.config.agent_config as ac
    import aiforge_core.runtime.vision as v
    monkeypatch.setattr(ac, "load_all", lambda: {"doer": {"model": "coder"}})
    monkeypatch.setattr(v, "supports_vision", lambda model: False)
    t = _ticket(metadata={"attached_files": [{"name": "a.png", "path": "/a.png"}]})
    assert orc._vision_block(t) == ""


def test_no_images_means_no_vision_block(vision):
    assert orc._vision_block(_ticket()) == ""


def test_a_broken_vision_probe_is_not_fatal(monkeypatch):
    import aiforge_core.config.agent_config as ac
    monkeypatch.setattr(ac, "load_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("no config")))
    assert orc._vision_block(_ticket()) == ""


def test_the_seed_prompt_carries_the_ticket_not_the_memory_block(monkeypatch):
    """The seed is replayed on every chat-mode call — 60-120x per ticket — so
    the memory brief goes into STATE instead."""
    monkeypatch.setattr(orc, "_operator_comments_block", lambda t: "")
    monkeypatch.setattr(orc, "_attachments_block", lambda t: "")
    monkeypatch.setattr(orc, "_vision_block", lambda t: "")
    monkeypatch.setattr(orc, "_playbook_prefix", lambda hay, cwd: ("", [], []))
    monkeypatch.setattr(orc, "_emit_context_injected", lambda t, s, w: None)
    out = orc._build_prompt(_ticket(), "MEMORY BLOCK")
    assert "# Ticket ONE-1" in out
    assert "Fix the parser" in out
    assert "MEMORY BLOCK" not in out


def test_a_body_less_ticket_still_builds(monkeypatch):
    monkeypatch.setattr(orc, "_operator_comments_block", lambda t: "")
    monkeypatch.setattr(orc, "_attachments_block", lambda t: "")
    monkeypatch.setattr(orc, "_vision_block", lambda t: "")
    monkeypatch.setattr(orc, "_playbook_prefix", lambda hay, cwd: ("", [], []))
    monkeypatch.setattr(orc, "_emit_context_injected", lambda t, s, w: None)
    assert "(no body)" in orc._build_prompt(_ticket(body=""), "")


# ─── per-ticket overrides ──────────────────────────────────────────────


def test_a_forced_provider_is_honoured(monkeypatch):
    import aiforge_core.config.agent_config as ac
    monkeypatch.setattr(ac, "PROVIDERS", {"openai_compatible": {}})
    t = _ticket(metadata={"force_provider": "openai_compatible"})
    assert orc._ticket_force_provider(t) == "openai_compatible"


def test_a_retired_provider_marker_is_ignored(monkeypatch):
    """A stale ticket must not crash the pipeline."""
    import aiforge_core.config.agent_config as ac
    monkeypatch.setattr(ac, "PROVIDERS", {"openai_compatible": {}})
    t = _ticket(metadata={"force_provider": "mlx_local"})
    assert orc._ticket_force_provider(t) is None


def test_no_override(monkeypatch):
    assert orc._ticket_force_provider(_ticket()) is None


# ─── external refs ─────────────────────────────────────────────────────


def test_external_refs_need_a_target_repo():
    t = _ticket(project="", metadata={"external_refs": ["https://x"]})
    assert orc._external_refs(t) == []


def test_only_string_refs_are_kept():
    t = _ticket(metadata={"external_refs": ["https://x", "  ", 42, None]})
    assert orc._external_refs(t) == ["https://x"]


def test_the_egress_gate_stops_ingestion(monkeypatch):
    monkeypatch.setenv("AIFORGE_EXTERNAL_INGEST", "0")
    monkeypatch.setattr(orc, "_external_refs",
                        lambda t: pytest.fail("read refs with the gate closed"))
    orc._ingest_ticket_external_refs(_ticket())


def test_ingestion_is_a_no_op_without_refs(monkeypatch):
    monkeypatch.setenv("AIFORGE_EXTERNAL_INGEST", "1")
    orc._ingest_ticket_external_refs(_ticket())


# ─── the verdict object ────────────────────────────────────────────────


def test_a_verdict_maps_to_a_ticket_status():
    v = orc._Verdict("pass", "all good")
    assert v.status == orc._VERDICT_TO_STATUS["pass"]
    v.demote("fail", "blocked", "empty diff")
    assert (v.outcome, v.status, v.reason) == ("fail", "blocked", "empty diff")


def test_a_demotion_can_keep_the_original_reason():
    v = orc._Verdict("partial", "ran out of budget")
    v.demote("partial", "in_review")
    assert v.reason == "ran out of budget"


def test_an_unknown_outcome_blocks():
    assert orc._Verdict("weird", "")._Verdict__dict__ if False else \
        orc._Verdict("weird", "").status == "blocked"


def test_the_enhancer_sentinel_blocks_before_the_planner_runs(monkeypatch):
    """It was documented in the prompt but never checked — a dead contract that
    let a garbage brief flow into a full run."""
    monkeypatch.setattr(orc, "_extract_verdict", lambda s: "pass")
    monkeypatch.setattr(orc, "_enhancer_block_reason", lambda s: "too vague")
    v = orc._initial_verdict(_ticket(), {})
    assert v.outcome == "fail"
    assert v.reason == "too vague"


def test_a_normal_run_keeps_its_verdict(monkeypatch):
    monkeypatch.setattr(orc, "_extract_verdict", lambda s: "pass")
    monkeypatch.setattr(orc, "_enhancer_block_reason", lambda s: None)
    monkeypatch.setattr(orc, "_extract_reason", lambda s, o: "looks right")
    v = orc._initial_verdict(_ticket(), {})
    assert (v.outcome, v.reason) == ("pass", "looks right")


@pytest.mark.parametrize("raw,expected", [
    ('{"verdict": "pass"}', {"verdict": "pass"}),
    ({"verdict": "pass"}, {"verdict": "pass"}),
    ("not json", {"raw": "not json"}),
    (None, None),
])
def test_the_validator_output_is_parsed_leniently(raw, expected):
    assert orc._validator_out({"validator_verdict": raw}) == expected


def test_no_state_has_no_validator_output():
    assert orc._validator_out(None) is None


# ─── the ground-truth demotions ────────────────────────────────────────


def test_a_test_only_diff_demotes_the_verdict():
    v = orc._Verdict("pass", "looks right")
    orc._apply_pr_demotions(_ticket(), v, {"pr_skip_reason": "test_only_diff",
                                           "test_only_files": ["t.py"]})
    assert (v.outcome, v.status) == ("fail", "blocked")


def test_a_pass_with_a_clean_tree_is_a_false_pass(monkeypatch):
    """ONE-163/164 both landed "done" with no commit — the Doer narrated an
    edit it never wrote and the feedback agent believed the prose."""
    monkeypatch.delenv("AIFORGE_ALLOW_EMPTY_PASS", raising=False)
    v = orc._Verdict("pass", "looks right")
    orc._apply_pr_demotions(_ticket(), v, {"pr_skip_reason": "no_changes"})
    assert (v.outcome, v.status) == ("fail", "blocked")
    assert "changed no files" in v.reason


def test_the_empty_pass_check_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_EMPTY_PASS", "1")
    v = orc._Verdict("pass", "looks right")
    orc._apply_pr_demotions(_ticket(), v, {"pr_skip_reason": "no_changes"})
    assert v.outcome == "pass"


def test_an_already_failing_verdict_is_not_re_demoted(monkeypatch):
    monkeypatch.delenv("AIFORGE_ALLOW_EMPTY_PASS", raising=False)
    v = orc._Verdict("fail", "the tests failed")
    orc._apply_pr_demotions(_ticket(), v, {"pr_skip_reason": "no_changes"})
    assert v.reason == "the tests failed"


def test_partial_work_with_a_pr_waits_for_a_human(monkeypatch):
    """The plateau cap: finished-but-imperfect work stops churning at the
    gate instead of replanning forever."""
    monkeypatch.delenv("AIFORGE_ALLOW_EMPTY_PASS", raising=False)
    v = orc._Verdict("partial", "plateaued")
    orc._apply_pr_demotions(_ticket(), v, {"pr_url": "https://github.com/x/1"})
    assert (v.outcome, v.status) == ("partial", "in_review")


def test_partial_work_with_nothing_to_review_stays_blocked(monkeypatch):
    monkeypatch.delenv("AIFORGE_ALLOW_EMPTY_PASS", raising=False)
    v = orc._Verdict("partial", "plateaued")
    orc._apply_pr_demotions(_ticket(), v, {})
    assert v.status == orc._VERDICT_TO_STATUS.get("partial", "blocked")


# ─── live verification + merge ─────────────────────────────────────────


@pytest.fixture
def live(monkeypatch):
    state = {"lv": {"ok": True}, "merged": []}
    monkeypatch.setattr(orc, "_run_live_verifier",
                        lambda ticket, url: state["lv"])
    import aiforge_core.runtime.git_pr as gp
    monkeypatch.setattr(gp, "merge_pr",
                        lambda url: state["merged"].append(url) or {"merged": True,
                                                                    "reason": ""})
    monkeypatch.delenv("AIFORGE_LIVE_VERIFIER", raising=False)
    monkeypatch.delenv("AIFORGE_AUTO_MERGE_ON_VALIDATE", raising=False)
    return state


def test_a_passing_live_verify_merges_the_pr(live):
    v = orc._Verdict("pass", "")
    pr = {"pr_url": "https://github.com/o/r/pull/1"}
    assert orc._live_verify(_ticket(), pr, v) == {"ok": True}
    assert live["merged"] == ["https://github.com/o/r/pull/1"]
    assert pr["pr_merged"] is True


def test_a_failing_live_verify_blocks_the_ticket(live):
    """The merged/worktree fix did not actually hold."""
    live["lv"] = {"ok": False, "rationale": "the endpoint still 500s"}
    v = orc._Verdict("pass", "")
    orc._live_verify(_ticket(), {"pr_url": "u"}, v)
    assert (v.outcome, v.status) == ("fail", "blocked")
    assert "still 500s" in v.reason
    assert live["merged"] == []


def test_no_pr_means_no_live_verify(live):
    monkeypatched = orc._live_verify(_ticket(), {}, orc._Verdict("pass", ""))
    assert monkeypatched is None


def test_a_failing_verdict_is_not_live_verified(live):
    assert orc._live_verify(_ticket(), {"pr_url": "u"},
                            orc._Verdict("fail", "")) is None


def test_the_live_verifier_can_be_turned_off(live, monkeypatch):
    monkeypatch.setenv("AIFORGE_LIVE_VERIFIER", "0")
    assert orc._live_verify(_ticket(), {"pr_url": "u"},
                            orc._Verdict("pass", "")) is None


def test_a_crashing_live_verifier_leaves_the_verdict_alone(live, monkeypatch):
    monkeypatch.setattr(orc, "_run_live_verifier",
                        lambda t, u: (_ for _ in ()).throw(RuntimeError("boom")))
    v = orc._Verdict("pass", "")
    assert orc._live_verify(_ticket(), {"pr_url": "u"}, v) is None
    assert v.outcome == "pass"


def test_auto_merge_on_validate_can_be_turned_off(live, monkeypatch):
    monkeypatch.setenv("AIFORGE_AUTO_MERGE_ON_VALIDATE", "0")
    orc._live_verify(_ticket(), {"pr_url": "u"}, orc._Verdict("pass", ""))
    assert live["merged"] == []


def test_an_already_merged_pr_is_still_surfaced(monkeypatch):
    import aiforge_core.runtime.git_pr as gp
    monkeypatch.setattr(gp, "merge_pr",
                        lambda url: {"merged": True, "reason": "already_merged"})
    pr: dict = {}
    orc._auto_merge(_ticket(), "u", pr)
    assert pr["pr_merge_reason"] == "already_merged"


def test_a_failing_merge_is_not_fatal(monkeypatch):
    import aiforge_core.runtime.git_pr as gp
    monkeypatch.setattr(gp, "merge_pr",
                        lambda url: (_ for _ in ()).throw(RuntimeError("gh down")))
    pr: dict = {}
    orc._auto_merge(_ticket(), "u", pr)
    assert pr == {}


# ─── CI + review ───────────────────────────────────────────────────────


def test_ci_is_graded_for_a_pushed_pr(monkeypatch):
    import aiforge_core.runtime.ci_feedback as ci
    monkeypatch.delenv("AIFORGE_CI_GRADE", raising=False)
    seen: dict = {}

    def _grade(url, poll_seconds=None):
        seen.update(url=url, poll=poll_seconds)
        return {"status": "success"}
    monkeypatch.setattr(ci, "grade_and_react", _grade)
    assert orc._grade_ci({"pr_url": "u"}) == {"status": "success"}
    assert seen["poll"] == 30


def test_no_pr_skips_ci_grading():
    assert orc._grade_ci({}) == {}


def test_ci_grading_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_CI_GRADE", "0")
    assert orc._grade_ci({"pr_url": "u"}) == {}


def test_a_ci_error_lands_in_metadata_rather_than_blocking(monkeypatch):
    import aiforge_core.runtime.ci_feedback as ci
    monkeypatch.delenv("AIFORGE_CI_GRADE", raising=False)
    monkeypatch.setattr(ci, "grade_and_react",
                        lambda url, poll_seconds=None: (_ for _ in ()).throw(
                            RuntimeError("gh rate limited")))
    out = orc._grade_ci({"pr_url": "u"})
    assert out["ok"] is False
    assert "rate limited" in out["error"]


def test_the_pr_is_reviewed_by_a_second_agent(monkeypatch):
    import aiforge_core.runtime.pr_reviewer as pr
    seen: dict = {}
    monkeypatch.setattr(pr, "review_pr",
                        lambda url, title, body: seen.update(url=url, title=title)
                        or {"ok": True, "verdict": "approve"})
    assert orc._review_pr_meta(_ticket(), {"pr_url": "u"})["verdict"] == "approve"
    assert seen["title"] == "Fix the parser"


def test_no_pr_skips_the_review():
    assert orc._review_pr_meta(_ticket(), {}) == {}


def test_a_failing_review_lands_in_metadata(monkeypatch):
    import aiforge_core.runtime.pr_reviewer as pr
    monkeypatch.setattr(pr, "review_pr",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("no model")))
    assert orc._review_pr_meta(_ticket(), {"pr_url": "u"})["ok"] is False


# ─── the status patch ──────────────────────────────────────────────────


def test_the_status_patch_folds_every_source(monkeypatch):
    monkeypatch.setattr(orc, "_extract_verifier", lambda s: "pass")
    out = orc._status_metadata(
        {}, orc._Verdict("pass", ""), {"pr_url": "u"},
        {"status": "success", "checks": ["build"]},
        {"ok": True, "verdict": "approve", "axes": {"scope": "ok"}},
        {"verdict": "approve", "rationale": "fine", "scope_ok": True,
         "regression_risk": "low"},
        {"ok": True, "rationale": "held up"})
    assert out["feedback_verdict"] == "pass"
    assert out["pr_url"] == "u"
    assert out["ci_status"] == "success"
    assert out["review_verdict"] == "approve"
    assert out["validator_scope_ok"] is True
    assert out["live_verifier_ok"] is True
    assert out["handled_by"] == "pipeline"


def test_absent_sources_add_no_keys(monkeypatch):
    monkeypatch.setattr(orc, "_extract_verifier", lambda s: None)
    out = orc._status_metadata({}, orc._Verdict("fail", ""), {}, {}, {}, None, None)
    assert "ci_status" not in out
    assert "review_verdict" not in out
    assert "validator_verdict" not in out
    assert "live_verifier_ok" not in out


def test_a_failed_review_contributes_nothing(monkeypatch):
    monkeypatch.setattr(orc, "_extract_verifier", lambda s: None)
    out = orc._status_metadata({}, orc._Verdict("fail", ""), {}, {},
                               {"ok": False, "error": "x"}, None, None)
    assert "review_verdict" not in out


# ─── workspace preparation ─────────────────────────────────────────────


def test_a_spec_scaffold_is_written_for_a_build_ticket(monkeypatch, tmp_path):
    import aiforge_core.runtime.spec_to_tests as s2t
    monkeypatch.delenv("AIFORGE_SPEC_TO_TESTS", raising=False)
    monkeypatch.setattr(orc, "_ticket_looks_readonly", lambda t: False)
    seen: dict = {}
    monkeypatch.setattr(s2t, "write_scaffold",
                        lambda ident, body, repo_root=None, language=None:
                        seen.update(ident=ident, language=language))
    orc._write_spec_scaffold(_ticket(), str(tmp_path))
    assert seen == {"ident": "ONE-1", "language": "python"}


def test_a_read_only_ticket_gets_no_scaffold(monkeypatch, tmp_path):
    """Otherwise an analysis ticket dirties the tree and lands a spurious PR."""
    import aiforge_core.runtime.spec_to_tests as s2t
    monkeypatch.delenv("AIFORGE_SPEC_TO_TESTS", raising=False)
    monkeypatch.setattr(orc, "_ticket_looks_readonly", lambda t: True)
    monkeypatch.setattr(s2t, "write_scaffold",
                        lambda *a, **k: pytest.fail("scaffolded a read-only ticket"))
    orc._write_spec_scaffold(_ticket(), str(tmp_path))


def test_the_scaffold_can_be_turned_off(monkeypatch, tmp_path):
    import aiforge_core.runtime.spec_to_tests as s2t
    monkeypatch.setenv("AIFORGE_SPEC_TO_TESTS", "0")
    monkeypatch.setattr(s2t, "write_scaffold",
                        lambda *a, **k: pytest.fail("scaffolded with the gate off"))
    orc._write_spec_scaffold(_ticket(), str(tmp_path))


def test_a_failing_scaffold_is_not_fatal(monkeypatch, tmp_path):
    import aiforge_core.runtime.spec_to_tests as s2t
    monkeypatch.delenv("AIFORGE_SPEC_TO_TESTS", raising=False)
    monkeypatch.setattr(orc, "_ticket_looks_readonly", lambda t: False)
    monkeypatch.setattr(s2t, "write_scaffold",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no disk")))
    orc._write_spec_scaffold(_ticket(), str(tmp_path))


def test_preparing_the_worktree_runs_every_step(monkeypatch):
    ran: list = []
    monkeypatch.setattr(orc, "_materialize_attachments_in_worktree",
                        lambda t, w: ran.append("attach"))
    monkeypatch.setattr(orc, "_persist_ticket_media", lambda t: ran.append("media"))
    monkeypatch.setattr(orc, "_write_spec_scaffold", lambda t, w: ran.append("spec"))
    monkeypatch.setattr(orc, "_ingest_ticket_external_refs",
                        lambda t: ran.append("refs"))
    orc._prepare_worktree(_ticket(), "/wt")
    assert ran == ["attach", "media", "spec", "refs"]


def test_a_failing_media_persist_never_breaks_the_loop(monkeypatch):
    monkeypatch.setattr(orc, "_materialize_attachments_in_worktree", lambda t, w: None)
    monkeypatch.setattr(orc, "_persist_ticket_media",
                        lambda t: (_ for _ in ()).throw(RuntimeError("no store")))
    monkeypatch.setattr(orc, "_write_spec_scaffold", lambda t, w: None)
    monkeypatch.setattr(orc, "_ingest_ticket_external_refs", lambda t: None)
    orc._prepare_worktree(_ticket(), "/wt")


# ─── deploy autonomy ───────────────────────────────────────────────────


@pytest.mark.parametrize("target", ["qa", "prod"])
def test_a_deploy_target_arms_auto_merge(monkeypatch, target):
    monkeypatch.delenv("AIFORGE_AUTO_MERGE", raising=False)
    orc._arm_deploy_env(_ticket(metadata={"deploy_target": target}))
    assert os.environ["AIFORGE_AUTO_MERGE"] == "1"
    assert os.environ["AIFORGE_DEPLOY_TARGET"] == target
    os.environ.pop("AIFORGE_AUTO_MERGE", None)
    os.environ.pop("AIFORGE_DEPLOY_TARGET", None)


def test_a_previous_runs_auto_merge_never_leaks_into_the_next(monkeypatch):
    monkeypatch.setenv("AIFORGE_AUTO_MERGE", "1")
    monkeypatch.setenv("AIFORGE_DEPLOY_TARGET", "prod")
    orc._arm_deploy_env(_ticket())
    assert "AIFORGE_AUTO_MERGE" not in os.environ
    assert "AIFORGE_DEPLOY_TARGET" not in os.environ


# ─── the local-model probe ─────────────────────────────────────────────


@pytest.mark.parametrize("base,loopback", [
    ("", True), ("http://127.0.0.1:1234/v1", True),
    ("http://localhost:1234/v1", True), ("https://cloud.example/v1", False),
])
def test_only_a_loopback_doer_is_probed(monkeypatch, base, loopback):
    import aiforge_core.config.agent_config as ac
    monkeypatch.setattr(ac, "get", lambda role: {"base_url": base})
    assert orc._doer_is_loopback() is loopback


def test_a_broken_config_reads_as_loopback(monkeypatch):
    import aiforge_core.config.agent_config as ac
    monkeypatch.setattr(ac, "get",
                        lambda role: (_ for _ in ()).throw(RuntimeError("bad")))
    assert orc._doer_is_loopback() is True


def test_a_remote_doer_is_never_probed(monkeypatch):
    monkeypatch.setattr(orc, "_doer_is_loopback", lambda: False)
    import aiforge_core.runtime.lm_health as lmh
    monkeypatch.setattr(lmh, "check_lm_health",
                        lambda restart_on_fail=False: pytest.fail("probed a remote"))
    orc._probe_local_lm(_ticket())


def test_the_health_probe_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(orc, "_doer_is_loopback", lambda: True)
    monkeypatch.setenv("AIFORGE_LM_HEALTH", "0")
    import aiforge_core.runtime.lm_health as lmh
    monkeypatch.setattr(lmh, "check_lm_health",
                        lambda **kw: pytest.fail("probed with the gate off"))
    orc._probe_local_lm(_ticket())


def test_an_unreachable_local_model_is_logged_not_fatal(monkeypatch):
    monkeypatch.setattr(orc, "_doer_is_loopback", lambda: True)
    monkeypatch.setenv("AIFORGE_LM_HEALTH", "1")
    import aiforge_core.runtime.lm_health as lmh
    monkeypatch.setattr(lmh, "check_lm_health",
                        lambda restart_on_fail=False: {"doer_ok": False,
                                                       "restarted": True})
    orc._probe_local_lm(_ticket())


def test_a_crashing_probe_is_swallowed(monkeypatch):
    monkeypatch.setattr(orc, "_doer_is_loopback", lambda: True)
    monkeypatch.setenv("AIFORGE_LM_HEALTH", "1")
    import aiforge_core.runtime.lm_health as lmh
    monkeypatch.setattr(lmh, "check_lm_health",
                        lambda **kw: (_ for _ in ()).throw(OSError("no socket")))
    orc._probe_local_lm(_ticket())


# ─── clarification parking ─────────────────────────────────────────────


def test_an_interactive_ticket_can_park_for_an_answer(monkeypatch):
    import aiforge_core.runtime.clarify as cl
    monkeypatch.setattr(cl, "maybe_clarify", lambda t: True)
    assert orc._clarify_parked(_ticket()) is True


def test_a_broken_clarify_gate_never_parks(monkeypatch):
    import aiforge_core.runtime.clarify as cl
    monkeypatch.setattr(cl, "maybe_clarify",
                        lambda t: (_ for _ in ()).throw(RuntimeError("no store")))
    assert orc._clarify_parked(_ticket()) is False


# ─── the poll loop ─────────────────────────────────────────────────────


@pytest.fixture
def loop(monkeypatch):
    state: dict = {"ticket": _ticket(), "worktree": "/wt", "ran": [],
                   "statuses": [], "parked": False}
    monkeypatch.setattr(orc.tickets_mod, "claim_next_any",
                        lambda: state["ticket"])
    monkeypatch.setattr(orc.tickets_mod, "update_status",
                        lambda tid, status, role=None, metadata_patch=None:
                        state["statuses"].append((status, metadata_patch)))
    monkeypatch.setattr(orc, "_clarify_parked", lambda t: state["parked"])
    monkeypatch.setattr(orc, "_probe_local_lm", lambda t: None)
    monkeypatch.setattr(orc, "_setup_ticket_workspace",
                        lambda t: (state["worktree"], {"env": "prior"}))
    monkeypatch.setattr(orc, "_restore_env", lambda env: state.setdefault("restored", env))
    monkeypatch.setattr(orc, "_prepare_worktree", lambda t, w: None)
    monkeypatch.setattr(orc, "_run_ticket",
                        lambda t, w: state["ran"].append(t.identifier))
    monkeypatch.setattr(orc, "set_force_provider",
                        lambda p: state.setdefault("forced", []).append(p))
    return state


def test_an_empty_queue_returns_false(loop):
    loop["ticket"] = None
    assert orc._process_one_ticket() is False


def test_a_claimed_ticket_runs_and_restores_the_environment(loop):
    assert orc._process_one_ticket() is True
    assert loop["ran"] == ["ONE-1"]
    assert loop["restored"] == {"env": "prior"}
    assert loop["forced"][-1] is None       # the override never leaks


def test_a_parked_ticket_does_no_work(loop):
    loop["parked"] = True
    assert orc._process_one_ticket() is True
    assert loop["ran"] == []


def test_a_ticket_with_no_target_repo_is_blocked(loop):
    loop["worktree"] = ""
    orc._process_one_ticket()
    status, patch = loop["statuses"][0]
    assert status == "blocked"
    assert "no target repo" in patch["error"]


def test_a_crashed_run_rescues_its_partial_work(loop, monkeypatch):
    """The Doer may have written real files before the orchestrator stalled."""
    monkeypatch.setattr(orc, "_run_ticket",
                        lambda t, w: (_ for _ in ()).throw(RuntimeError("adk died")))
    monkeypatch.setattr(orc, "_rescue_partial_work",
                        lambda t: {"pr_url": "https://github.com/o/r/pull/9"})
    assert orc._process_one_ticket() is True
    status, patch = loop["statuses"][0]
    assert status == "blocked"
    assert patch["pr_url"].endswith("/9")
    assert "adk died" in patch["error"]


def test_a_failed_status_write_still_restores_the_environment(loop, monkeypatch):
    monkeypatch.setattr(orc, "_run_ticket",
                        lambda t, w: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(orc, "_rescue_partial_work", lambda t: {})
    monkeypatch.setattr(orc.tickets_mod, "update_status",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert orc._process_one_ticket() is True
    assert loop["restored"] == {"env": "prior"}


def test_the_rescue_reports_its_pr(monkeypatch):
    monkeypatch.setattr(orc, "commit_push_open_pr",
                        lambda t: {"pr_url": "https://github.com/o/r/pull/9"})
    assert orc._rescue_partial_work(_ticket())["pr_url"].endswith("/9")


def test_a_failing_rescue_returns_nothing(monkeypatch):
    monkeypatch.setattr(orc, "commit_push_open_pr",
                        lambda t: (_ for _ in ()).throw(RuntimeError("no git")))
    assert orc._rescue_partial_work(_ticket()) == {}


def test_a_failure_logs_concisely_by_default(monkeypatch, caplog):
    monkeypatch.delenv("AIFORGE_ADK_TRACEBACKS", raising=False)
    with caplog.at_level("ERROR"):
        orc._log_run_failure(_ticket(), RuntimeError("model down"))
    assert "RuntimeError: model down" in caplog.text
    assert "Traceback" not in caplog.text


def test_tracebacks_can_be_restored_for_a_novel_failure(monkeypatch, caplog):
    monkeypatch.setenv("AIFORGE_ADK_TRACEBACKS", "1")
    try:
        raise ValueError("something new")
    except ValueError as exc:
        with caplog.at_level("ERROR"):
            orc._log_run_failure(_ticket(), exc)
    assert "Traceback" in caplog.text


# ─── main ──────────────────────────────────────────────────────────────


@pytest.fixture
def entry(monkeypatch):
    from aiforge_core.config import backends
    from aiforge_core.runtime import memory_sources as ms
    state: dict = {"processed": True, "reaped": [], "boot": []}
    monkeypatch.setattr(backends, "require_data_backends", lambda: None)
    monkeypatch.setattr(backends, "boot_log", lambda: state["boot"].append(1))
    monkeypatch.setattr(orc.tickets_mod, "reap_stale_in_progress",
                        lambda: state["reaped"])
    monkeypatch.setattr(ms, "reap_stale_indexing", lambda lease: [])
    monkeypatch.setattr(orc, "_process_one_ticket", lambda: state["processed"])
    monkeypatch.setattr(orc.time, "sleep", lambda s: state.setdefault("slept", s))
    return state


def test_a_ticket_run_announces_the_backends(entry):
    assert orc.main() == 0
    assert entry["boot"] == [1]


def test_an_idle_poll_backs_off_quietly(entry, monkeypatch):
    entry["processed"] = False
    monkeypatch.setenv("AIFORGE_POLL_IDLE_S", "3")
    assert orc.main() == 0
    assert entry["boot"] == []
    assert entry["slept"] == 3


def test_orphaned_tickets_are_requeued_before_claiming(entry):
    """Re-claim only selects 'todo', so a crash-orphaned in_progress ticket
    would stay stuck forever."""
    entry["reaped"] = ["ONE-9"]
    assert orc.main() == 0


def test_a_reaper_hiccup_never_blocks_the_poll(entry, monkeypatch):
    monkeypatch.setattr(orc.tickets_mod, "reap_stale_in_progress",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert orc.main() == 0


def test_a_stale_index_reaper_hiccup_is_also_soft(entry, monkeypatch):
    from aiforge_core.runtime import memory_sources as ms
    monkeypatch.setattr(ms, "reap_stale_indexing",
                        lambda lease: (_ for _ in ()).throw(RuntimeError("locked")))
    assert orc.main() == 0
