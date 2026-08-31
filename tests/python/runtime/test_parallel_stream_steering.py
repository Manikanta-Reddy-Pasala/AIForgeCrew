"""Steering a parallel build mid-run, and reporting honestly at the end.

A comment the user makes while a build is running is treated as a MANDATE, not
a hint: it is pinned to the subtask it names (or to SPEC.md globally when it
names none), so the remaining subtasks AND the final reconcile have to satisfy
it — and the user is told which reading was taken, because a steer routed to
the wrong place silently is worse than one that is refused.

The end-of-run verdict is the other half. ``ok`` is three-valued, and the
None case used to claim "no toolchain" even when one WAS installed and simply
reported errors, so the stack is detected before saying that. A failing test is
reported as possibly a bad TEST — a local model writes those too — rather than
asserting the implementation is wrong. And the detailed integration report is
only attached when it agrees with the authoritative verdict, or the answer
contradicts itself in two consecutive paragraphs.
"""
from __future__ import annotations

import os
import queue

import pytest

from aiforge_core.runtime.parallel_subtasks import _stream as S


@pytest.fixture(autouse=True)
def clean_mandates():
    S._USER_MANDATES.clear()
    yield
    S._USER_MANDATES.clear()


def _subs():
    return [{"slug": "lexer", "path": "pkg/lexer.py", "goal": "tokenise"},
            {"slug": "parser", "path": "pkg/parser.py", "goal": "parse"}]


# ─── routing one steer ─────────────────────────────────────────────────


def test_a_steer_naming_a_subtask_is_pinned_to_it(tmp_path):
    subs = _subs()
    out = S._pin_to_subtask(subs, "lexer", "handle unicode", "")
    heading, feedback = out
    assert "MANDATORY user instruction" in subs[0]["goal"]
    assert subs[0]["_user_mandate"] == ["handle unicode"]
    assert "pkg/lexer.py" in heading and "pkg/lexer.py" in feedback


def test_the_analysis_note_is_shown_back_to_the_user(tmp_path):
    _, feedback = S._pin_to_subtask(_subs(), "lexer", "x", "matched by path")
    assert "matched by path" in feedback


def test_a_steer_naming_a_subtask_that_is_gone_is_not_pinned():
    assert S._pin_to_subtask(_subs(), "ghost", "x", "") is None


@pytest.mark.parametrize("target,marker", [("new", "NEW requirement"),
                                           ("global", "whole build")])
def test_a_steer_with_no_subtask_goes_to_the_spec(target, marker):
    heading, feedback = S._steer_headings(target, "")
    assert marker in heading and "✅ Got it" in feedback


def test_a_mandate_is_appended_to_the_spec_and_remembered(tmp_path):
    (tmp_path / S._SPEC_MD).write_text("# SPEC\n")
    S._append_spec_mandate(str(tmp_path), "## ⚙ heading", "use postgres")
    body = (tmp_path / S._SPEC_MD).read_text()
    assert "## ⚙ heading" in body and "**MUST:** use postgres" in body
    assert S._USER_MANDATES[str(tmp_path)] == ["use postgres"], \
        "so the reconcile prompt can re-assert it"


def test_an_unwritable_spec_still_records_the_mandate(tmp_path):
    S._append_spec_mandate(str(tmp_path / "nope"), "## h", "use postgres")
    assert S._USER_MANDATES[str(tmp_path / "nope")] == ["use postgres"]


@pytest.fixture()
def router(monkeypatch):
    state: dict = {"route": {"target": "lexer", "note": "names the lexer"}}
    monkeypatch.setattr(S, "_route_steering",
                        lambda text, subs: state["route"])
    return state


def test_a_routed_steer_lands_on_its_subtask_and_the_spec(router, tmp_path):
    subs = _subs()
    feedback = S._apply_steer("handle unicode", subs, str(tmp_path))
    assert "pkg/lexer.py" in feedback
    assert "MANDATORY" in subs[0]["goal"]
    assert "handle unicode" in (tmp_path / S._SPEC_MD).read_text()


def test_a_global_steer_lands_on_the_spec_alone(router, tmp_path):
    router["route"] = {"target": "global", "note": ""}
    subs = _subs()
    feedback = S._apply_steer("use postgres", subs, str(tmp_path))
    assert "whole build" in feedback
    assert all("MANDATORY" not in s["goal"] for s in subs)


def test_a_steer_for_a_vanished_subtask_falls_back_to_global(router, tmp_path):
    router["route"] = {"target": "ghost", "note": ""}
    assert "whole build" in S._apply_steer("x", _subs(), str(tmp_path))


# ─── draining steers mid-run ───────────────────────────────────────────


@pytest.fixture()
def interject(monkeypatch, router):
    from aiforge_core.runtime import chat_interject, chat_steer
    state: dict = {"pending": True, "queued": ["handle unicode"]}
    monkeypatch.setattr(chat_interject, "pending",
                        lambda sid: state["pending"])
    monkeypatch.setattr(chat_interject, "drain", lambda sid: state["queued"])
    monkeypatch.setattr(chat_steer, "steer_event",
                        lambda text: {"type": "steer", "text": text})
    return state


def test_a_steer_is_echoed_and_then_acknowledged(interject, tmp_path):
    evs = list(S._steering_drain(7, _subs(), str(tmp_path)))
    assert evs[0] == {"type": "steer", "text": "handle unicode"}
    assert evs[1]["role"] == "planner" and "✅ Got it" in evs[1]["text"]


def test_nothing_queued_means_nothing_to_do(interject, tmp_path):
    interject["pending"] = False
    assert list(S._steering_drain(7, _subs(), str(tmp_path))) == []


def test_a_blank_steer_is_ignored(interject, tmp_path):
    interject["queued"] = ["   "]
    assert list(S._steering_drain(7, _subs(), str(tmp_path))) == []


def test_a_run_with_no_session_cannot_be_steered(interject, tmp_path):
    assert list(S._steering_drain(None, _subs(), str(tmp_path))) == []


def test_a_broken_steer_queue_never_breaks_the_build(interject, tmp_path,
                                                     monkeypatch):
    from aiforge_core.runtime import chat_interject
    monkeypatch.setattr(chat_interject, "pending",
                        lambda sid: (_ for _ in ()).throw(OSError("x")))
    assert list(S._steering_drain(7, _subs(), str(tmp_path))) == []


# ─── stop ──────────────────────────────────────────────────────────────


def test_stop_is_seen_by_the_run(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: True)
    assert S._cancel_checker_for(7)() is True


def test_a_sessionless_run_is_never_cancelled():
    assert S._cancel_checker_for(None)() is False


def test_a_broken_cancel_check_reads_as_not_cancelled(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "is_cancelled",
                        lambda sid: (_ for _ in ()).throw(OSError("x")))
    assert S._cancel_checker_for(7)() is False


def test_the_session_is_armed_for_steering_and_stop(monkeypatch):
    from aiforge_core.runtime import chat_cancel, chat_interject
    seen: dict = {}
    monkeypatch.setattr(chat_interject, "set_steerable",
                        lambda sid, v: seen.update(steerable=(sid, v)))
    monkeypatch.setattr(chat_cancel, "set_active",
                        lambda sid: seen.update(active=sid))
    S._arm_session(7)
    assert seen == {"steerable": (7, True), "active": 7}


def test_arming_a_sessionless_run_is_a_no_op(monkeypatch):
    from aiforge_core.runtime import chat_interject
    monkeypatch.setattr(chat_interject, "set_steerable",
                        lambda sid, v: pytest.fail("armed a sessionless run"))
    S._arm_session(None)


def test_an_unarmable_session_does_not_break_the_run(monkeypatch):
    from aiforge_core.runtime import chat_interject
    monkeypatch.setattr(chat_interject, "set_steerable",
                        lambda sid, v: (_ for _ in ()).throw(OSError("x")))
    S._arm_session(7)


# ─── streaming the run ─────────────────────────────────────────────────


def test_events_stream_until_the_runner_finishes(interject, tmp_path):
    interject["pending"] = False
    q: queue.Queue = queue.Queue()
    q.put({"type": "subtask_update", "slug": "lexer"})
    q.put(None)
    evs = list(S._drain_run(q, 7, _subs(), str(tmp_path), lambda: False))
    assert [e["type"] for e in evs] == ["subtask_update"]


def test_a_steer_is_folded_in_between_events(interject, tmp_path):
    q: queue.Queue = queue.Queue()
    q.put({"type": "subtask_update", "slug": "lexer"})
    q.put(None)
    evs = list(S._drain_run(q, 7, _subs(), str(tmp_path), lambda: False))
    assert [e["type"] for e in evs] == ["subtask_update", "steer", "thought"]


def test_stop_ends_the_stream_immediately(interject, tmp_path):
    interject["pending"] = False
    q: queue.Queue = queue.Queue()
    q.put({"type": "subtask_update"})
    q.put({"type": "subtask_update"})
    evs = list(S._drain_run(q, 7, _subs(), str(tmp_path), lambda: True))
    assert len(evs) == 1, "the runner winds itself down on should_cancel"


# ─── the honest verdict ────────────────────────────────────────────────


def test_a_green_build_says_so(tmp_path):
    assert S._build_verdict(True, str(tmp_path)) == "✅ **Built — all tests pass.**"


def test_a_failing_test_may_be_a_bad_test(tmp_path):
    """A local model writes those too, so do not assert the code is wrong."""
    out = S._build_verdict(False, str(tmp_path))
    assert "some tests still fail" in out and "incorrect tests" in out


def test_an_unclear_result_with_a_toolchain_present_names_it(tmp_path,
                                                             monkeypatch):
    """Saying 'no toolchain' when one IS installed and simply errored was a
    lie the report used to tell."""
    from aiforge_core.runtime.tools import project_runner
    monkeypatch.setattr(project_runner, "detect",
                        lambda cwd: {"stacks": ["python", "maven"]})
    out = S._build_verdict(None, str(tmp_path))
    assert "python, maven" in out and "did NOT pass cleanly" in out


def test_an_unclear_result_with_no_toolchain_says_where_to_run_it(tmp_path,
                                                                  monkeypatch):
    from aiforge_core.runtime.tools import project_runner
    monkeypatch.setattr(project_runner, "detect", lambda cwd: {"stacks": []})
    out = S._build_verdict(None, str(tmp_path))
    assert "Couldn't run the tests on this host" in out


def test_an_undetectable_stack_is_not_fatal(tmp_path, monkeypatch):
    from aiforge_core.runtime.tools import project_runner
    monkeypatch.setattr(project_runner, "detect",
                        lambda cwd: (_ for _ in ()).throw(OSError("x")))
    assert S._detected_stacks(str(tmp_path)) == []


# ─── tidying the delivered tree ────────────────────────────────────────


def test_off_plan_files_are_removed_before_integration(monkeypatch, tmp_path):
    """A worker-invented package that duplicates a declared module turns into
    collection errors."""
    monkeypatch.setattr(S, "_prune_offplan_files",
                        lambda cwd, subs: ["pkg/phantom.py"])
    evs = list(S._prune_offplan(str(tmp_path), _subs()))
    assert "Removed 1 off-plan file(s)" in evs[0]["text"]
    assert "pkg/phantom.py" in evs[0]["text"]


def test_a_clean_tree_says_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_prune_offplan_files", lambda cwd, subs: [])
    assert list(S._prune_offplan(str(tmp_path), _subs())) == []


def test_a_failing_prune_never_breaks_the_run(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_prune_offplan_files",
                        lambda cwd, subs: (_ for _ in ()).throw(OSError("x")))
    assert list(S._prune_offplan(str(tmp_path), _subs())) == []


def test_the_mergers_sidecars_are_not_delivered(tmp_path):
    d = tmp_path / S._CONTRACT_DIR
    d.mkdir(parents=True)
    (d / "blackboard.json").write_text("{}")
    S._clean_contract_sidecars(str(tmp_path))
    assert not d.exists()


def test_cleaning_a_workspace_with_no_sidecars_is_quiet(tmp_path):
    S._clean_contract_sidecars(str(tmp_path))


# ─── the final report ──────────────────────────────────────────────────


@pytest.fixture()
def finalize(monkeypatch):
    state: dict = {"verdict": "every requirement addressed",
                   "res": {"ok": True}, "rep": {"ok": True, "md": "# report"},
                   "changes": [{"type": "changes", "files": 3}]}
    monkeypatch.setattr(S, "_verify_against_spec",
                        lambda cwd, spec: state["verdict"])

    def _integration(cwd, res, should_cancel=None):
        res.update(state["res"])
        res["rep"] = state["rep"]
        yield {"type": "thought", "role": "verifier", "text": "building…"}
    monkeypatch.setattr(S, "_reconcile_integration", _integration)
    monkeypatch.setattr(S, "_emit_changes",
                        lambda cwd, sha, **kw: iter(state["changes"]))
    return state


def _final(tmp_path, agg=None):
    return list(S._finalize(str(tmp_path), _subs(), "# SPEC",
                            agg or {"done": 2, "total": 2}, "sha0",
                            lambda: False))


def test_the_merged_result_is_verified_against_the_spec(finalize, tmp_path):
    evs = _final(tmp_path)
    assert "Verifying the merged result against SPEC.md" in evs[0]["text"]
    assert evs[1]["text"] == "every requirement addressed"


def test_the_diff_and_the_summary_close_the_run(finalize, tmp_path):
    evs = _final(tmp_path)
    assert any(e.get("type") == "changes" for e in evs)
    assert "**Pipeline complete** — 2/2" in evs[-1]["text"]
    assert "all tests pass" in evs[-1]["text"]


def test_the_detailed_report_is_attached_when_it_agrees(finalize, tmp_path):
    assert "# report" in _final(tmp_path)[-1]["text"]


def test_a_report_that_contradicts_the_verdict_is_left_off(finalize, tmp_path):
    """Otherwise the answer says 'all tests pass' and then 'tests failed' from
    a different runner that missed a dependency."""
    finalize["rep"] = {"ok": False, "md": "# report says failed"}
    assert "# report says failed" not in _final(tmp_path)[-1]["text"]


def test_the_reconciles_own_runner_is_authoritative(finalize, tmp_path):
    finalize["res"] = {"ok": False}
    finalize["rep"] = {"ok": True, "md": "# report"}
    assert "some tests still fail" in _final(tmp_path)[-1]["text"]


def test_a_failed_verification_never_blocks_the_result(finalize, tmp_path,
                                                       monkeypatch):
    monkeypatch.setattr(S, "_verify_against_spec",
                        lambda cwd, spec: (_ for _ in ()).throw(OSError("x")))
    assert "**Pipeline complete**" in _final(tmp_path)[-1]["text"]


def test_a_failed_integration_still_reports(finalize, tmp_path, monkeypatch):
    def _boom(cwd, res, should_cancel=None):
        raise RuntimeError("reconcile blew up")
        yield  # pragma: no cover
    monkeypatch.setattr(S, "_reconcile_integration", _boom)
    assert "**Pipeline complete**" in _final(tmp_path)[-1]["text"]


def test_a_failed_diff_still_reports(finalize, tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_emit_changes",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("git")))
    assert "**Pipeline complete**" in _final(tmp_path)[-1]["text"]


def test_a_long_verification_verdict_is_trimmed(finalize, tmp_path):
    finalize["verdict"] = "z" * 2000
    assert len(_final(tmp_path)[1]["text"]) == 1500
