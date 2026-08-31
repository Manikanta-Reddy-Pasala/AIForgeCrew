"""Reading a PR's CI back, and what happens when it is red.

The runner should not hand the world back to a human the moment a PR exists,
so it grades the PR's check runs itself through the ``gh`` CLI — no SDK, no
webhooks, and a soft failure whenever gh is missing, unauthenticated or rate
limited, because none of those are reasons to break a run.

The grading rule is deliberately pessimistic: anything still running outranks
both colours (a green read taken before the slow job reports is worse than no
read at all), and one red beats every green. Auto-rollback is opt-in — closing
someone's PR on a flaky check is worse than leaving it red.

One fixed bug is pinned here: grade_and_react carries pr_url and repo forward
itself, because the check-run snapshot has neither, and the CI-autofix request
was being built with both fields empty.
"""
from __future__ import annotations

import json
import types as pytypes

import pytest

from aiforge_core.runtime import ci_feedback as CI

_PR = "https://github.com/acme/widgets/pull/42"


@pytest.fixture()
def gh(monkeypatch):
    """A scripted ``gh``: canned JSON per API path, recorded argv."""
    state: dict = {
        "calls": [], "have_gh": True, "rc": 0, "stderr": "",
        "pr": {"head": {"sha": "abc123"}},
        "checks": {"check_runs": [{"name": "build", "status": "completed",
                                   "conclusion": "success",
                                   "output": {"summary": "all good"}}]},
        "raw": None, "timeout_on": None,
    }
    monkeypatch.setattr(CI.shutil, "which",
                        lambda name: "/usr/bin/gh" if state["have_gh"] else None)

    def _run(argv, **kw):
        state["calls"].append(list(argv))
        joined = " ".join(argv)
        if state["timeout_on"] and state["timeout_on"] in joined:
            raise CI.subprocess.TimeoutExpired("gh", 20)
        if state["raw"] is not None:
            return pytypes.SimpleNamespace(returncode=state["rc"],
                                           stdout=state["raw"],
                                           stderr=state["stderr"])
        payload = state["checks"] if "check-runs" in joined else state["pr"]
        return pytypes.SimpleNamespace(returncode=state["rc"],
                                       stdout=json.dumps(payload),
                                       stderr=state["stderr"])
    monkeypatch.setattr(CI.subprocess, "run", _run)
    return state


# ─── which PR ──────────────────────────────────────────────────────────


def test_a_pr_url_is_split_into_owner_repo_and_number():
    assert CI._parse_pr_url(_PR) == ("acme", "widgets", "42")


@pytest.mark.parametrize("url", ["", "https://github.com/acme/widgets",
                                 "https://gitlab.com/a/b/pull/1", None])
def test_anything_that_is_not_a_pr_url_is_refused(url):
    assert CI._parse_pr_url(url) is None


def test_reading_checks_for_a_non_pr_says_so(gh):
    res = CI.read_pr_checks("not-a-url")
    assert res["error"] == "bad_pr_url" and gh["calls"] == []


# ─── grading the check runs ────────────────────────────────────────────


def test_all_green_is_green():
    assert CI._aggregate([{"status": "completed", "conclusion": "success"},
                          {"status": "completed", "conclusion": "skipped"}]) \
        == ("green", True)


def test_one_red_beats_every_green():
    assert CI._aggregate([{"status": "completed", "conclusion": "success"},
                          {"status": "completed", "conclusion": "failure"}]) \
        == ("red", True)


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled"])
def test_every_red_conclusion_counts_as_red(conclusion):
    assert CI._aggregate([{"status": "completed",
                           "conclusion": conclusion}])[0] == "red"


def test_a_job_still_running_outranks_both_colours():
    """A green read taken before the slow job reports is worse than none."""
    assert CI._aggregate([{"status": "completed", "conclusion": "failure"},
                          {"status": "in_progress", "conclusion": None}]) \
        == ("pending", False)


def test_no_checks_at_all_is_unknown():
    assert CI._aggregate([]) == ("unknown", True)


def test_a_conclusion_nobody_recognises_is_unknown():
    assert CI._aggregate([{"status": "completed",
                           "conclusion": "action_required"}]) == ("unknown", True)


# ─── reading them off GitHub ───────────────────────────────────────────


def test_the_checks_are_read_for_the_head_commit(gh):
    res = CI.read_pr_checks(_PR)
    assert res["ok"] is True and res["status"] == "green"
    assert res["checks"][0] == {"name": "build", "conclusion": "success",
                                "summary": "all good"}
    assert "repos/acme/widgets/pulls/42" in " ".join(gh["calls"][0])
    assert "commits/abc123/check-runs" in " ".join(gh["calls"][1])


def test_the_reported_checks_are_capped(gh):
    gh["checks"] = {"check_runs": [{"name": f"c{i}", "status": "completed",
                                    "conclusion": "success"}
                                   for i in range(30)]}
    res = CI.read_pr_checks(_PR)
    assert res["raw_count"] == 30 and len(res["checks"]) == 20


def test_a_long_check_summary_is_trimmed(gh):
    gh["checks"] = {"check_runs": [{"name": "c", "status": "completed",
                                    "conclusion": "failure",
                                    "output": {"summary": "x" * 500}}]}
    assert len(CI.read_pr_checks(_PR)["checks"][0]["summary"]) == 300


def test_without_gh_installed_nothing_breaks(gh):
    gh["have_gh"] = False
    assert CI.read_pr_checks(_PR) == {"ok": False, "error": "missing_gh"}


def test_an_auth_failure_bubbles_up_with_its_stderr(gh):
    gh["rc"] = 1
    gh["stderr"] = "gh: authentication required"
    res = CI.read_pr_checks(_PR)
    assert res["error"] == "gh_failed" and "authentication" in res["stderr"]


def test_a_hung_gh_call_is_a_timeout_not_a_hang(gh):
    gh["timeout_on"] = "pulls/42"
    assert CI.read_pr_checks(_PR) == {"ok": False, "error": "timeout"}


def test_unparseable_output_is_reported_per_call(gh):
    gh["raw"] = "not json"
    assert CI.read_pr_checks(_PR)["error"] == "bad_json"


def test_a_pr_with_no_head_commit_cannot_be_graded(gh):
    gh["pr"] = {"head": {}}
    assert CI.read_pr_checks(_PR)["error"] == "no_head_sha"


def test_a_failing_check_runs_call_is_labelled_separately(gh, monkeypatch):
    calls = {"n": 0}
    real = CI._gh_json

    def _gh_json(args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"head": {"sha": "abc"}}, None
        return real(args, **kw)
    monkeypatch.setattr(CI, "_gh_json", _gh_json)
    gh["rc"] = 1
    assert CI.read_pr_checks(_PR)["error"] == "gh_checks_failed"


# ─── grading, and rolling back ─────────────────────────────────────────


@pytest.fixture()
def graded(monkeypatch):
    state: dict = {"snaps": [{"ok": True, "status": "green", "raw_count": 1,
                              "checks": []}], "reverted": 0, "slept": []}
    monkeypatch.setattr(CI, "read_pr_checks",
                        lambda url: state["snaps"][0] if len(state["snaps"]) == 1
                        else state["snaps"].pop(0))
    monkeypatch.setattr(CI, "open_revert_pr",
                        lambda url: state.update(reverted=state["reverted"] + 1)
                        or {"ok": True})
    monkeypatch.setattr(CI.time, "sleep", lambda s: state["slept"].append(s))
    return state


def test_the_pr_and_repo_are_carried_into_the_result(graded):
    """The snapshot has neither, and the autofix request was being built with
    both fields empty."""
    out = CI.grade_and_react(_PR)
    assert out["pr_url"] == _PR and out["repo"] == "acme/widgets"
    assert out["rolled_back"] is False


def test_a_green_pr_is_left_alone(graded, monkeypatch):
    monkeypatch.setenv("AIFORGE_CI_AUTO_ROLLBACK", "1")
    assert CI.grade_and_react(_PR)["rolled_back"] is False
    assert graded["reverted"] == 0


def test_rollback_is_opt_in(graded, monkeypatch):
    """Closing someone's PR on a flaky check is worse than leaving it red."""
    monkeypatch.delenv("AIFORGE_CI_AUTO_ROLLBACK", raising=False)
    graded["snaps"] = [{"ok": True, "status": "red", "raw_count": 1,
                        "checks": []}]
    assert CI.grade_and_react(_PR)["rolled_back"] is False
    assert graded["reverted"] == 0


def test_red_with_rollback_on_closes_the_pr(graded):
    graded["snaps"] = [{"ok": True, "status": "red", "raw_count": 1,
                        "checks": []}]
    out = CI.grade_and_react(_PR, auto_rollback=True)
    assert out["rolled_back"] is True and out["rollback_result"] == {"ok": True}


def test_a_read_that_failed_is_returned_as_is(graded):
    graded["snaps"] = [{"ok": False, "error": "missing_gh"}]
    assert CI.grade_and_react(_PR) == {"ok": False, "error": "missing_gh"}


def test_grading_waits_for_the_first_check_to_appear(graded):
    """Called right after a push, there are no check runs yet."""
    graded["snaps"] = [{"ok": True, "status": "unknown", "raw_count": 0},
                       {"ok": True, "status": "green", "raw_count": 2,
                        "checks": []}]
    out = CI.grade_and_react(_PR, poll_seconds=30)
    assert out["status"] == "green" and graded["slept"] == [5]


def test_the_wait_is_capped_so_the_runner_is_never_blocked(graded,
                                                           monkeypatch):
    graded["snaps"] = [{"ok": True, "status": "unknown", "raw_count": 0}]
    times = iter([0.0, 0.0, 1000.0, 1000.0])
    monkeypatch.setattr(CI.time, "time", lambda: next(times))
    out = CI.grade_and_react(_PR, poll_seconds=9999)
    assert out["status"] == "unknown"


# ─── closing the PR ────────────────────────────────────────────────────


def test_the_rollback_comments_before_it_closes(gh):
    res = CI.open_revert_pr(_PR)
    assert res["ok"] is True
    comment, close = gh["calls"][0], gh["calls"][1]
    assert comment[1:3] == ["pr", "comment"] and "auto-rollback" in comment[-1]
    assert close[1:3] == ["pr", "close"]


def test_a_rollback_that_could_not_close_is_not_ok(gh):
    gh["rc"] = 1
    res = CI.open_revert_pr(_PR)
    assert res["ok"] is False and res["close_ok"] is False


def test_a_rollback_needs_a_real_pr_url_and_gh(gh):
    assert CI.open_revert_pr("nope")["error"] == "bad_pr_url"
    gh["have_gh"] = False
    assert CI.open_revert_pr(_PR)["error"] == "missing_gh"


# ─── the follow-up fix request ─────────────────────────────────────────


def _failed(n=1, summary="pytest: 3 failed"):
    return [{"name": f"check-{i}", "conclusion": "failure",
             "summary": summary} for i in range(n)]


def test_the_request_names_the_failing_checks():
    req = CI.build_fix_request(_PR, "acme/widgets", _failed(2))
    assert req["kind"] == "ci_fix" and req["checks"] == ["check-0", "check-1"]
    assert "check-0" in req["title"] and "pytest: 3 failed" in req["body"]
    assert req["pr"] == _PR and req["repo"] == "acme/widgets"


def test_a_wall_of_failures_does_not_become_the_title():
    req = CI.build_fix_request(_PR, "r", _failed(9))
    assert "(+4 more)" in req["title"]


def test_the_log_excerpts_share_one_budget():
    """The body carries roughly 2KB of log, however many checks failed."""
    req = CI.build_fix_request(_PR, "r", _failed(4, summary="z" * 1500))
    assert "…(truncated)" in req["body"]
    assert len(req["body"]) < 2600


def test_a_check_with_no_summary_is_still_listed():
    req = CI.build_fix_request(_PR, "r", [{"conclusion": "failure"}])
    assert "- (unnamed)" in req["body"] and req["checks"] == ["(unnamed)"]


# ─── closing the loop ──────────────────────────────────────────────────


def test_a_green_grade_dispatches_nothing(monkeypatch):
    monkeypatch.setenv("AIFORGE_CI_AUTOFIX_ENABLED", "1")
    assert CI.on_ci_red({"checks": [{"name": "a", "conclusion": "success"}]}) \
        is None


def test_the_request_is_built_even_when_autofix_is_off(monkeypatch):
    """The caller can still inspect what would have been dispatched."""
    monkeypatch.setenv("AIFORGE_CI_AUTOFIX_ENABLED", "0")
    sent: list = []
    req = CI.on_ci_red({"pr_url": _PR, "repo": "acme/widgets",
                        "checks": _failed()}, dispatch=sent.append)
    assert req["checks"] == ["check-0"] and sent == []


def test_with_autofix_on_the_request_is_dispatched(monkeypatch):
    monkeypatch.setenv("AIFORGE_CI_AUTOFIX_ENABLED", "1")
    sent: list = []
    CI.on_ci_red({"pr_url": _PR, "repo": "r", "checks": _failed()},
                 dispatch=sent.append)
    assert sent and sent[0]["kind"] == "ci_fix"


def test_a_dispatch_that_blows_up_does_not_break_grading(monkeypatch, caplog):
    monkeypatch.setenv("AIFORGE_CI_AUTOFIX_ENABLED", "1")
    req = CI.on_ci_red(
        {"pr_url": _PR, "checks": _failed()},
        dispatch=lambda r: (_ for _ in ()).throw(RuntimeError("queue down")))
    assert req is not None


def test_only_the_red_checks_reach_the_request(monkeypatch):
    monkeypatch.setenv("AIFORGE_CI_AUTOFIX_ENABLED", "0")
    req = CI.on_ci_red({"checks": [{"name": "ok", "conclusion": "success"},
                                   {"name": "bad", "conclusion": "timed_out"}]})
    assert req["checks"] == ["bad"]
