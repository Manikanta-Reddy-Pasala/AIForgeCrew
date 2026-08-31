"""The PR-comment loop: turn review feedback into work, exactly once.

A one-shot timer job, so idempotence is the whole design. The last-seen
comment id per PR is persisted under the shared config dir rather than raw
home — on a container that state lives on the mounted volume, and losing it
means every already-handled comment tickets itself again on the next restart.
A comment is only marked seen once its ticket actually posted, so a failed
POST is retried rather than silently dropped.

Not every comment deserves a pipeline run, either: questions and nits get a
lightweight acknowledgement, and only a change request becomes a ticket. The
old always-ticket behaviour is still one env flag away.
"""
from __future__ import annotations

import json
import types as pytypes

import pytest

from aiforge_core.runtime import pr_comments_loop as P


# ─── classifying one comment ───────────────────────────────────────────


@pytest.mark.parametrize("body", ["Why is this a while loop?",
                                  "Can we extract this?",
                                  "should this be async",
                                  "What does this flag do?"])
def test_a_question_is_recognised(body):
    assert P.classify_comment(body) == "question"


@pytest.mark.parametrize("body", ["nit: trailing whitespace",
                                  "typo in the docstring",
                                  "please rename this variable",
                                  "lint is unhappy here"])
def test_a_nit_is_recognised(body):
    assert P.classify_comment(body) == "nit"


@pytest.mark.parametrize("body", ["This leaks a file handle on the error path.",
                                  "Guard against a null id here."])
def test_anything_else_is_a_change_request(body):
    assert P.classify_comment(body) == "change_request"


@pytest.mark.parametrize("body", ["", "   ", None])
def test_an_empty_comment_is_treated_as_work(body):
    """Better to raise a ticket than to swallow a comment we cannot read."""
    assert P.classify_comment(body) == "change_request"


def test_a_question_word_with_punctuation_still_counts():
    assert P.classify_comment("Why. this is not obvious") == "question"


# ─── routing it ────────────────────────────────────────────────────────


@pytest.mark.parametrize("body,mode", [
    ("Why this loop?", "lightweight"),
    ("nit: spacing", "lightweight"),
    ("This breaks on an empty list.", "full"),
])
def test_only_a_change_request_gets_the_full_pipeline(body, mode, monkeypatch):
    monkeypatch.delenv("AIFORGE_PR_COMMENT_LIGHTWEIGHT", raising=False)
    route = P.route_comment({"id": 5, "body": body})
    assert route["mode"] == mode and route["comment_id"] == 5


def test_the_old_always_ticket_behaviour_is_one_flag_away(monkeypatch):
    monkeypatch.setenv("AIFORGE_PR_COMMENT_LIGHTWEIGHT", "0")
    route = P.route_comment({"id": 5, "body": "nit: spacing"})
    assert route["mode"] == "full" and route["reason"] == "lightweight_disabled:nit"


# ─── the lightweight acknowledgement ───────────────────────────────────


def test_a_question_is_answered_as_a_clarification():
    out = P.lightweight_reply({"id": 1, "body": "Why this loop?"})
    assert out["kind"] == "question" and "clarification" in out["reply_text"]
    assert "Why this loop?" in out["reply_text"]


def test_a_nit_is_acknowledged_as_a_minor_follow_up():
    out = P.lightweight_reply({"id": 1, "body": "nit: spacing"})
    assert out["kind"] == "nit" and "nit/style" in out["reply_text"]


def test_nothing_is_posted_to_github_yet():
    assert P.lightweight_reply({"id": 1, "body": "why?"})["posted"] is False


def test_only_the_first_line_is_quoted_back():
    out = P.lightweight_reply({"id": 1, "body": "nit: one\nand a long tail"})
    assert "and a long tail" not in out["reply_text"]


def test_a_bodyless_comment_still_produces_a_reply():
    assert P.lightweight_reply({"id": 1})["reply_text"]


# ─── the seen-comment state ────────────────────────────────────────────


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    p = tmp_path / "pr_comments_seen.json"
    monkeypatch.setattr(P, "_STATE_PATH", p)
    return p


def test_state_round_trips(state_file):
    P._save_state({"acme/widgets#1": 99})
    assert P._load_state() == {"acme/widgets#1": 99}


def test_a_first_run_starts_from_nothing(state_file):
    assert P._load_state() == {}


def test_corrupt_state_is_ignored_rather_than_fatal(state_file):
    state_file.write_text("{not json")
    assert P._load_state() == {}


def test_an_unwritable_state_dir_only_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(P, "_STATE_PATH", tmp_path / "nope" / "s.json")
    monkeypatch.setattr(P.Path, "mkdir",
                        lambda self, **kw: (_ for _ in ()).throw(OSError("ro")))
    P._save_state({"a": 1})  # no raise


# ─── talking to gh ─────────────────────────────────────────────────────


@pytest.fixture()
def gh(monkeypatch):
    state: dict = {"rc": 0, "out": "[]", "timeout": False, "calls": []}

    def _run(argv, **kw):
        state["calls"].append(list(argv))
        if state["timeout"]:
            raise P.subprocess.TimeoutExpired("gh", 30)
        out = state["out"]
        if callable(out):
            out = out(argv)
        return pytypes.SimpleNamespace(returncode=state["rc"], stdout=out,
                                       stderr="")
    monkeypatch.setattr(P.subprocess, "run", _run)
    return state


def test_json_comes_back_parsed(gh):
    gh["out"] = json.dumps([{"id": 1}])
    assert P._gh_json(["api", "x"]) == [{"id": 1}]


@pytest.mark.parametrize("break_it", ["rc", "timeout", "garbage"])
def test_any_gh_failure_is_simply_no_data(gh, break_it):
    if break_it == "rc":
        gh["rc"] = 1
    elif break_it == "timeout":
        gh["timeout"] = True
    else:
        gh["out"] = "not json"
    assert P._gh_json(["api", "x"]) is None


# ─── one PR's comments ─────────────────────────────────────────────────


def _pr(number=7, repo="widgets", owner="acme"):
    return {"number": number, "headRepository": {"name": repo},
            "headRepositoryOwner": {"login": owner}}


@pytest.fixture()
def flow(monkeypatch):
    """Comments come back from gh; tickets are posted through a stub."""
    state: dict = {"comments": [], "posted": [], "post_ok": True}
    monkeypatch.setattr(P, "_gh_json", lambda args: state["comments"])

    def _post(*, project, pr_num, comment):
        state["posted"].append((project, pr_num, comment.get("id")))
        return state["post_ok"]
    monkeypatch.setattr(P, "_post_followup_ticket", _post)
    monkeypatch.delenv("AIFORGE_PR_COMMENT_LIGHTWEIGHT", raising=False)
    return state


def _tally():
    return {"new_tickets": 0, "lightweight_replies": 0}


def test_a_change_request_becomes_a_ticket(flow):
    flow["comments"] = [{"id": 10, "body": "This breaks on empty input."}]
    seen, tally = {}, _tally()
    P._process_pr_comments(_pr(), seen, tally)
    assert tally["new_tickets"] == 1 and flow["posted"] == [("widgets", 7, 10)]
    assert seen == {"acme/widgets#7": 10}


def test_a_question_is_answered_without_a_ticket(flow):
    flow["comments"] = [{"id": 10, "body": "Why this loop?"}]
    seen, tally = {}, _tally()
    P._process_pr_comments(_pr(), seen, tally)
    assert tally == {"new_tickets": 0, "lightweight_replies": 1}
    assert seen == {"acme/widgets#7": 10} and flow["posted"] == []


def test_a_comment_already_handled_is_never_handled_twice(flow):
    """This is the whole point of the state file."""
    flow["comments"] = [{"id": 10, "body": "fix it"}, {"id": 11, "body": "and this"}]
    seen, tally = {"acme/widgets#7": 10}, _tally()
    P._process_pr_comments(_pr(), seen, tally)
    assert flow["posted"] == [("widgets", 7, 11)]


def test_a_comment_whose_ticket_failed_is_retried_next_run(flow):
    """It is only marked seen once the ticket actually posted."""
    flow["post_ok"] = False
    flow["comments"] = [{"id": 10, "body": "fix it"}]
    seen, tally = {}, _tally()
    P._process_pr_comments(_pr(), seen, tally)
    assert seen == {} and tally["new_tickets"] == 0


def test_a_pr_with_no_repository_is_skipped(flow):
    flow["comments"] = [{"id": 10, "body": "fix it"}]
    seen = {}
    P._process_pr_comments({"number": 7, "headRepository": {}}, seen, _tally())
    assert flow["posted"] == [] and seen == {}


def test_an_unreadable_comment_list_is_skipped(flow):
    flow["comments"] = None
    seen = {}
    P._process_pr_comments(_pr(), seen, _tally())
    assert seen == {}


# ─── the ticket POST ───────────────────────────────────────────────────


@pytest.fixture()
def api(monkeypatch):
    import urllib.request
    state: dict = {"status": 201, "seen": {}, "raise": None}

    class _Resp:
        status = 201

        def __enter__(self):
            _Resp.status = state["status"]
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=None, context=None):
        if state["raise"]:
            raise state["raise"]
        state["seen"] = {"url": req.full_url,
                         "payload": json.loads(req.data.decode()),
                         "method": req.method}
        return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return state


def test_the_ticket_carries_the_comment_and_its_link(api, monkeypatch):
    monkeypatch.setenv("AIFORGE_API_BASE", "http://api:8799")
    ok = P._post_followup_ticket(
        project="widgets", pr_num=7,
        comment={"id": 10, "body": "fix the leak",
                 "user": {"login": "reviewer"},
                 "html_url": "https://github.com/acme/widgets/pull/7#c10"})
    assert ok is True
    payload = api["seen"]["payload"]
    assert api["seen"]["url"] == "http://api:8799/api/tickets"
    assert payload["project"] == "widgets" and payload["labels"] == ["pr-followup"]
    assert payload["metadata"] == {"pr_followup": True, "pr_number": 7,
                                   "comment_id": 10}
    assert "reviewer" in payload["body"] and "#c10" in payload["body"]


def test_a_giant_comment_is_truncated_into_the_ticket(api):
    P._post_followup_ticket(project="w", pr_num=1,
                            comment={"id": 1, "body": "x" * 9000})
    assert api["seen"]["payload"]["body"].count("x") == 4000


def test_anything_but_created_is_a_failure(api):
    api["status"] = 200
    assert P._post_followup_ticket(project="w", pr_num=1,
                                   comment={"id": 1}) is False


def test_an_unreachable_api_does_not_crash_the_timer_job(api):
    api["raise"] = OSError("connection refused")
    assert P._post_followup_ticket(project="w", pr_num=1,
                                   comment={"id": 1}) is False


# ─── the one-shot run ──────────────────────────────────────────────────


@pytest.fixture()
def loop(monkeypatch, state_file):
    state: dict = {"have_gh": True, "prs": [_pr(), _pr(8)], "processed": []}
    monkeypatch.setattr(P.shutil, "which",
                        lambda n: "/usr/bin/gh" if state["have_gh"] else None)
    monkeypatch.setattr(P, "_gh_json", lambda args: state["prs"])

    def _process(pr, seen, tally):
        state["processed"].append(pr["number"])
        seen[f"pr{pr['number']}"] = 1
        tally["new_tickets"] += 1
    monkeypatch.setattr(P, "_process_pr_comments", _process)
    return state


def test_every_open_pr_we_authored_is_scanned(loop, state_file):
    out = P.run()
    assert out == {"ok": True, "tickets_created": 2, "lightweight_replies": 0,
                   "prs_scanned": 2}
    assert loop["processed"] == [7, 8]
    assert json.loads(state_file.read_text()) == {"pr7": 1, "pr8": 1}


def test_no_open_prs_is_a_clean_no_op(loop):
    loop["prs"] = None
    assert P.run()["prs_scanned"] == 0


def test_without_gh_the_job_reports_instead_of_failing(loop):
    loop["have_gh"] = False
    assert P.run() == {"ok": False, "error": "missing_gh"}


def test_the_entrypoint_prints_json_and_exits_on_its_result(loop, capsys):
    assert P.main() == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_a_failed_run_exits_nonzero(loop, capsys):
    loop["have_gh"] = False
    assert P.main() == 1
