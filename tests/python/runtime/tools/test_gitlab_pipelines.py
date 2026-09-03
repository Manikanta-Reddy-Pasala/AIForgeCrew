"""GitLab CI pipeline tools: list, read one, and watch one to completion.

The GitLab API is faked at ``_request`` — the layer directly under every tool
and directly above the shared HTTP helper — so these exercise the addressing,
the status vocabulary, the job/failure reporting and the watch loop, without a
network and without re-testing urllib.
"""
import pytest

from aiforge_core.runtime.tools import gitlab


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.test")
    monkeypatch.setenv("GITLAB_TOKEN", "t0ken")
    monkeypatch.delenv("GITLAB_PROJECT", raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    yield


def _pipe(pid=101, status="success", ref="main", **kw):
    d = {"id": pid, "iid": 9, "status": status, "ref": ref,
         "sha": "abcdef1234567890", "source": "push", "duration": 92,
         "created_at": "2026-08-24T10:00:00Z", "updated_at": "2026-08-24T10:02:00Z",
         "web_url": f"https://gitlab.test/g/p/-/pipelines/{pid}"}
    d.update(kw)
    return d


def _job(name="build", status="success", jid=1, **kw):
    d = {"id": jid, "name": name, "stage": "test", "status": status,
         "duration": 12.5, "allow_failure": False,
         "web_url": f"https://gitlab.test/g/p/-/jobs/{jid}"}
    d.update(kw)
    return d


def _fake_request(monkeypatch, routes, calls=None):
    """Route GET path -> payload. ``routes`` values may be a list, in which
    case each call pops the next one (so a watch sees a pipeline progress)."""
    def _req(method, path, params=None, body=None, **kw):
        if calls is not None:
            # kwargs too: `body_cap` is a real behaviour (the trace needs its
            # own large one), and a fake that swallows it into **kw makes the
            # test that checks it a tautology.
            calls.append((method, path, params, kw))
        # Match on the END of the path, not anywhere in it.
        # `/projects/x/pipelines/101/jobs` CONTAINS `/pipelines/101`, so
        # substring routing answered a jobs request with a pipeline record —
        # a fake that lies in exactly the shape of the code under test, which
        # is the one kind of fake worth being strict about.
        hit = None
        for key in sorted(routes, key=len, reverse=True):
            if path.endswith(key):
                hit = key
                break
        if hit is None:
            return {"ok": False, "error": "http 404", "detail": path}
        val = routes[hit]
        if isinstance(val, list):
            return val.pop(0) if len(val) > 1 else val[0]
        return val
    monkeypatch.setattr(gitlab, "_request", _req)


# ── listing ─────────────────────────────────────────────────────────

def test_pipelines_lists_newest_first(monkeypatch):
    _fake_request(monkeypatch, {"/pipelines": {
        "ok": True, "data": [_pipe(103, "running"), _pipe(102, "failed")]}})
    out = gitlab.gitlab_pipelines({"project": "g/p"})
    assert out["ok"]
    assert [p["id"] for p in out["pipelines"]] == [103, 102]
    assert out["pipelines"][0]["status"] == "running"
    # A short sha to read, the full one to feed back into a `sha=` filter.
    assert out["pipelines"][0]["sha_short"] == "abcdef123456"
    assert out["pipelines"][0]["sha"] == "abcdef1234567890"


def test_pipelines_needs_a_project(monkeypatch):
    _fake_request(monkeypatch, {})
    out = gitlab.gitlab_pipelines({})
    assert not out["ok"]
    assert "project" in out["error"]
    assert "GITLAB_PROJECT" in out["hint"]


def test_an_unknown_status_filter_is_refused_not_sent(monkeypatch):
    """GitLab 400s on an unknown status value, and a 400 for the whole call
    reads to the agent as "GitLab is broken" rather than "you typo'd"."""
    calls: list = []
    _fake_request(monkeypatch, {"/pipelines": {"ok": True, "data": []}}, calls)
    out = gitlab.gitlab_pipelines({"project": "g/p", "status": "green"})
    assert not out["ok"]
    assert "green" in out["error"]
    assert "success" in out["hint"]
    assert not calls, "the bad filter was sent to GitLab anyway"


def test_the_british_spelling_is_translated(monkeypatch):
    """GitLab spells it `canceled`. Accepting `cancelled` and passing it
    through unchanged returns an empty list that looks like "nothing was
    cancelled"."""
    calls: list = []
    _fake_request(monkeypatch, {"/pipelines": {"ok": True, "data": []}}, calls)
    gitlab.gitlab_pipelines({"project": "g/p", "status": "cancelled"})
    assert calls[0][2]["status"] == "canceled"


# ── reading one ─────────────────────────────────────────────────────

def test_pipeline_by_id_reports_jobs_and_verdict(monkeypatch):
    _fake_request(monkeypatch, {
        "/pipelines/101": {"ok": True, "data": _pipe(101, "success")},
        "/jobs": {"ok": True, "data": [_job("build"), _job("test", jid=2)]},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 101})
    assert out["ok"]
    assert out["passed"]
    assert out["finished"]
    assert [j["name"] for j in out["jobs"]] == ["build", "test"]
    assert out["failed_jobs"] == []


def test_a_failed_pipeline_brings_the_log_of_what_failed(monkeypatch):
    """The tail of the failing job's log is the whole reason to ask."""
    _fake_request(monkeypatch, {
        "/pipelines/102": {"ok": True, "data": _pipe(102, "failed")},
        "/jobs/7/trace": {"ok": True, "data": "...\\nE   assert 1 == 2\\n"},
        "/jobs": {"ok": True, "data": [_job("build"),
                                       _job("test", "failed", jid=7)]},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 102})
    assert out["ok"]                       # the READ worked…
    assert out["passed"] is False          # …the pipeline did not
    assert out["failed_jobs"] == ["test"]
    assert "assert 1 == 2" in out["logs"]["test"]


def test_an_allow_failure_job_is_not_blamed(monkeypatch):
    """A job that failed with allow_failure did NOT fail the pipeline.
    Blaming it sends someone to debug a job that is working as configured."""
    _fake_request(monkeypatch, {
        "/pipelines/104": {"ok": True, "data": _pipe(104, "success")},
        "/jobs": {"ok": True, "data": [
            _job("build"),
            _job("lint", "failed", jid=8, allow_failure=True)]},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 104})
    assert out["passed"] is True
    assert out["failed_jobs"] == []
    assert out.get("logs") in (None, {})


def test_a_capped_log_list_says_it_was_capped(monkeypatch):
    """Never let a cap look like completeness."""
    jobs = [_job(f"t{i}", "failed", jid=i) for i in range(1, 7)]
    _fake_request(monkeypatch, {
        "/pipelines/105": {"ok": True, "data": _pipe(105, "failed")},
        "/trace": {"ok": True, "data": "boom"},
        "/jobs": {"ok": True, "data": jobs},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 105})
    assert len(out["failed_jobs"]) == 6
    assert len(out["logs"]) == gitlab._MAX_TRACES
    assert "6 jobs failed" in out["logs_truncated"]


def test_a_ref_resolves_to_the_latest_pipeline_on_that_branch(monkeypatch):
    calls: list = []
    _fake_request(monkeypatch, {
        "/pipelines/103": {"ok": True, "data": _pipe(103, "running", "feat/x")},
        "/pipelines": {"ok": True, "data": [_pipe(103, "running", "feat/x")]},
        "/jobs": {"ok": True, "data": []},
    }, calls)
    out = gitlab.gitlab_pipeline({"project": "g/p", "ref": "feat/x"})
    assert out["id"] == 103
    assert calls[0][2]["ref"] == "feat/x"
    assert calls[0][2]["per_page"] == 1


def test_no_pipeline_for_a_ref_says_so_plainly(monkeypatch):
    _fake_request(monkeypatch, {"/pipelines": {"ok": True, "data": []}})
    out = gitlab.gitlab_pipeline({"project": "g/p", "ref": "nope"})
    assert not out["ok"]
    assert out["error"] == "no_pipelines"
    assert "nope" in out["hint"]


def test_a_manual_pipeline_is_finished_but_not_passed(monkeypatch):
    """`manual` means it is blocked on a job somebody has to click. Calling it
    active makes a watch wait out its whole budget for a human; calling it a
    success is a lie."""
    _fake_request(monkeypatch, {
        "/pipelines/106": {"ok": True, "data": _pipe(106, "manual")},
        "/jobs": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 106})
    assert out["finished"] is True
    assert out["passed"] is False
    assert out["blocked_on_manual"] is True


def test_an_unknown_status_keeps_watching_rather_than_claiming_success(monkeypatch):
    """GitLab adds statuses. The failure modes are not symmetric: treating an
    unknown one as active costs one more poll, treating it as terminal tells
    the user their deploy passed."""
    _fake_request(monkeypatch, {
        "/pipelines/107": {"ok": True, "data": _pipe(107, "quantum_pending")},
        "/jobs": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 107})
    assert out["finished"] is False
    assert out["passed"] is False


def test_a_jobs_error_does_not_lose_the_pipeline(monkeypatch):
    _fake_request(monkeypatch, {
        "/pipelines/108": {"ok": True, "data": _pipe(108, "success")},
        "/jobs": {"ok": False, "error": "http 403"},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 108})
    assert out["ok"]
    assert out["passed"]
    assert out["jobs_error"] == "http 403"


# ── watching ────────────────────────────────────────────────────────

@pytest.fixture
def _clock(monkeypatch):
    """A clock the test drives.

    Faking ONLY `sleep` leaves `monotonic` frozen, so `elapsed + interval >
    budget` can never fire and the time budget is never the thing that ends a
    loop — a test asserting "the budget stopped it" then passes because of an
    entirely different guard. Advance the clock BY the sleep."""
    import time as _t
    state = {"t": 1000.0, "slept": []}
    monkeypatch.setattr(_t, "monotonic", lambda: state["t"])

    def _sleep(sec):
        state["slept"].append(sec)
        state["t"] += sec
    monkeypatch.setattr(_t, "sleep", _sleep)
    return state


@pytest.fixture
def _attended(monkeypatch):
    """A live session id, so the watch is NOT silently short-leashed.

    Under pytest `chat_cancel.active()` returns None, which clamps every watch
    to the unattended 10 checks — quietly making that the reason any loop
    ended, whatever the test meant to exercise."""
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "active", lambda: "sess-test")
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: False)


def test_watch_returns_the_moment_it_finishes(monkeypatch, _clock, _attended):
    _fake_request(monkeypatch, {
        "/pipelines/109": [
            {"ok": True, "data": _pipe(109, "running")},
            {"ok": True, "data": _pipe(109, "running")},
            {"ok": True, "data": _pipe(109, "success")},
        ],
        "/jobs": {"ok": True, "data": [_job("build")]},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 109, "interval_s": 5})
    assert out["ok"]
    assert out["passed"]
    assert out["checks"] == 3
    assert "elapsed_s" in out


def test_watch_reports_the_failure_with_its_log(monkeypatch, _clock, _attended):
    _fake_request(monkeypatch, {
        "/pipelines/110": [
            {"ok": True, "data": _pipe(110, "running")},
            {"ok": True, "data": _pipe(110, "failed")},
        ],
        "/jobs/3/trace": {"ok": True, "data": "ImportError: no module named x"},
        "/jobs": {"ok": True, "data": [_job("test", "failed", jid=3)]},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 110, "interval_s": 5})
    assert out["ok"]                 # the watch worked…
    assert out["passed"] is False    # …the pipeline failed
    assert "ImportError" in out["logs"]["test"]


def test_watch_gives_up_without_calling_it_a_failure(monkeypatch, _clock,
                                                     _attended):
    """A watch that runs out of budget has learned nothing about the pipeline.
    Reporting that as failed would be inventing an outcome."""
    _fake_request(monkeypatch, {
        "/pipelines/111": {"ok": True, "data": _pipe(111, "running")},
        "/jobs": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 111, "interval_s": 5,
         "timeout_s": 20, "max_checks": 50})
    assert out["ok"]
    assert out["timed_out"] is True
    assert out["passed"] is False
    assert out["finished"] is False
    assert "still running" in out["reason"]
    # THE TIME BUDGET is what ended it — 20s at 5s intervals — not max_checks
    # and not the unattended clamp. With a frozen clock this read 50 and 10
    # respectively, and the assertion passed for neither of its stated reasons.
    # 5 polls: t=0,5,10,15,20 — the sixth would need t=25, past the budget.
    assert out["checks"] == 5, out["checks"]
    assert _clock["t"] - 1000.0 <= 20.0


def test_watch_stops_on_a_permanent_error_instead_of_burning_the_budget(
        monkeypatch, _clock, _attended):
    """A bad token fails identically forever."""
    calls: list = []
    _fake_request(monkeypatch, {"/pipelines": {"ok": False, "error": "http 401"}},
                  calls)
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "ref": "main", "interval_s": 5})
    assert not out["ok"]
    assert out["checks"] == 1


def test_watch_rides_over_a_rate_limit(monkeypatch, _clock, _attended):
    """429 is the most transient failure there is — waiting is the remedy."""
    _fake_request(monkeypatch, {
        "/pipelines/112": [
            {"ok": False, "error": "http 429"},
            {"ok": True, "data": _pipe(112, "success")},
        ],
        "/jobs": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 112, "interval_s": 5})
    assert out["ok"]
    assert out["passed"]
    assert out["checks"] == 2


def test_the_unattended_SECONDS_budget_also_bounds_the_run(monkeypatch, _clock):
    """The sibling test uses interval_s=5, so the 10-CHECK cap always binds
    first and the seconds clamp — the half its docstring is about — was never
    exercised. Deleting the seconds clamp outright left the suite green."""
    _fake_request(monkeypatch, {
        "/pipelines/144": {"ok": True, "data": _pipe(144, "running")},
        "/jobs": {"ok": True, "data": []},
    })
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "active", lambda: None)
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 144, "interval_s": 60,
         "timeout_s": 1800, "max_checks": 500})
    # 4 polls: t=0,60,120,180 — the fifth would need t=240, past the 180s
    # budget. Well under the 10-check cap, so the SECONDS clamp is what ended
    # it, which is the half this test exists for.
    assert out["checks"] == 4, out["checks"]
    assert out["unattended_budget_s"] == 180
    assert "unattended_max_checks" not in out


def test_the_clamp_that_BIT_is_the_one_reported(monkeypatch, _clock):
    """Reporting the seconds budget unconditionally read as "the 180s ran out"
    when what actually ended the run was the 10-check cap at 45s — and a caller
    that asked for 60 checks and got 10 had no field to explain itself."""
    _fake_request(monkeypatch, {
        "/pipelines/145": {"ok": True, "data": _pipe(145, "running")},
        "/jobs": {"ok": True, "data": []},
    })
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "active", lambda: None)
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 145, "interval_s": 5,
         "timeout_s": 1800, "max_checks": 500})
    assert out["checks"] == 10
    assert out["unattended_max_checks"] == 10
    assert "unattended_budget_s" not in out
    assert out["elapsed_s"] < 180


def test_an_attended_watch_reports_no_clamp_at_all(monkeypatch, _clock,
                                                   _attended):
    _fake_request(monkeypatch, {
        "/pipelines/146": {"ok": True, "data": _pipe(146, "running")},
        "/jobs": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 146, "interval_s": 5,
         "timeout_s": 20})
    assert "unattended_budget_s" not in out
    assert "unattended_max_checks" not in out


def test_a_marker_only_log_reads_as_empty_not_as_two_carriage_returns(
        monkeypatch):
    """A job that ran no commands, or whose output was entirely folded, is
    non-blank RAW and empty once the markers are stripped. Checking emptiness
    before cleaning returned "\r\r" as the failure log, with no note."""
    _fake_request(monkeypatch, {
        "/pipelines/147": {"ok": True, "data": _pipe(147, "failed")},
        "/trace": {"ok": True,
                   "data": "section_start:1:b\r\x1b[0Ksection_end:2:b\r\x1b[0K"},
        "/jobs": {"ok": True, "data": [_job("t", "failed", jid=1)]},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 147})
    assert "empty" in out["logs"]["t"]


def test_a_jobs_error_hint_names_the_error_it_knows(monkeypatch):
    """`failed_jobs: []` after a 403 is an UNREAD list, not an empty one.
    Offering three speculative causes while the actual error sits two keys
    above sends the reader to debug a .gitlab-ci.yml that is fine."""
    _fake_request(monkeypatch, {
        "/pipelines/148": {"ok": True, "data": _pipe(148, "failed")},
        "/jobs": {"ok": False, "error": "http 403"},
        "/bridges": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 148})
    assert "http 403" in out["hint"]
    assert "not absent from it" in out["hint"]


def test_a_malformed_iid_beats_the_empty_body_check(monkeypatch):
    """"no fields to update" for a malformed iid is true and useless."""
    _fake_request(monkeypatch, {"": {"ok": True, "data": {}}})
    out = gitlab.gitlab_update({"project": "g/p", "iid": "../../x"})
    assert "bad iid" in out["error"]


def test_an_unattended_watch_gets_a_short_leash(monkeypatch, _clock):
    """Stop is gated on a session id, and chat_cancel is a ContextVar that does
    not cross into a worker thread. With no cancel handle nothing can interrupt
    this loop, so it fails SHORT rather than failing open."""
    _fake_request(monkeypatch, {
        "/pipelines/113": {"ok": True, "data": _pipe(113, "running")},
        "/jobs": {"ok": True, "data": []},
    })
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "active", lambda: None)
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 113, "interval_s": 5,
         "timeout_s": 1800, "max_checks": 500})
    assert out["checks"] <= 10


def test_a_stopped_watch_says_stopped(monkeypatch, _clock):
    _fake_request(monkeypatch, {
        "/pipelines/114": {"ok": True, "data": _pipe(114, "running")},
        "/jobs": {"ok": True, "data": []},
    })
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "active", lambda: "sess-1")
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: True)
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 114, "interval_s": 5})
    assert out["stopped"] is True
    assert not out["ok"]


def test_a_stop_during_the_sleep_is_not_reported_as_success(monkeypatch, _clock):
    """THE REGRESSION. The stopped return spread `**last` AFTER its literals,
    so once one poll had succeeded `last["ok"]` overwrote `ok: False` and a
    watch the user cancelled came back as one that worked — with
    `error: "stopped by user"` sitting next to `ok: True`. An error dict
    overwrote the message too, making Stop indistinguishable from a failure.

    The old test cancelled on the FIRST call, when `last` is still {} and the
    override is invisible: the test asserting the invariant was blind to the
    only case that violates it."""
    _fake_request(monkeypatch, {
        "/pipelines/115": {"ok": True, "data": _pipe(115, "running")},
        "/jobs": {"ok": True, "data": []},
    })
    from aiforge_core.runtime import chat_cancel
    calls = {"n": 0}

    def _cancelled(sid):
        calls["n"] += 1
        return calls["n"] > 1          # let the first poll through, stop in the sleep
    monkeypatch.setattr(chat_cancel, "active", lambda: "sess-1")
    monkeypatch.setattr(chat_cancel, "is_cancelled", _cancelled)
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 115, "interval_s": 5})
    assert out["stopped"] is True
    assert out["ok"] is False, "a cancelled watch reported success"
    assert out["error"] == "stopped by user"
    assert out["status"] == "running"     # …and the last snapshot survives


def test_a_watch_that_never_read_the_pipeline_is_not_ok(monkeypatch, _clock,
                                                        _attended):
    """Ending the budget on transient errors used to return ok:True with an
    HTTP error and NO `passed` key — a successful-looking envelope about a
    pipeline that was never observed. That is the shape from which a model
    tells someone their build passed."""
    _fake_request(monkeypatch, {"/pipelines": {"ok": False, "error": "http 503"}})
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "ref": "main", "interval_s": 5, "timeout_s": 20})
    assert out["ok"] is False
    assert out["error"] == "http 503"
    assert "never successfully read" in out["reason"]


def test_a_watch_that_ends_on_a_blip_keeps_the_last_good_data(monkeypatch,
                                                              _clock, _attended):
    """It DID read the pipeline, four polls ago. Report that, flagged stale —
    overwriting `last` unconditionally threw the known status away."""
    _fake_request(monkeypatch, {
        "/pipelines/116": [
            {"ok": True, "data": _pipe(116, "running")},
            {"ok": False, "error": "http 429"},
        ],
        "/jobs": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 116, "interval_s": 5, "timeout_s": 20})
    assert out["ok"]
    assert out["timed_out"]
    assert out["status"] == "running"
    assert out["passed"] is False
    assert out["stale"] is True
    assert out["last_poll_error"] == "http 429"


def test_a_transport_blip_does_not_abort_the_watch(monkeypatch, _clock,
                                                   _attended):
    """URLError/TimeoutError/OSError come back as `str(exc)`, so a retryable
    set enumerated as "429 or 5xx" matched none of them — and a connection
    reset, the single likeliest failure in a ten-minute watch of a self-hosted
    GitLab, killed the whole watch on first occurrence."""
    _fake_request(monkeypatch, {
        "/pipelines/117": [
            {"ok": False,
             "error": "<urlopen error [Errno 54] Connection reset by peer>"},
            {"ok": True, "data": _pipe(117, "success")},
        ],
        "/jobs": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 117, "interval_s": 5})
    assert out["ok"]
    assert out["passed"]
    assert out["checks"] == 2


def test_a_ref_watch_pins_the_pipeline_it_started_on(monkeypatch, _clock,
                                                     _attended):
    """A colleague pushing mid-watch created a NEWER pipeline on the same ref,
    the watch silently re-targeted it, and when the new one passed it reported
    `passed` for a pipeline the user never asked about — while theirs failed."""
    calls: list = []
    _fake_request(monkeypatch, {
        "/pipelines": {"ok": True, "data": [_pipe(500, "running")]},
        "/pipelines/500": [
            {"ok": True, "data": _pipe(500, "running")},
            {"ok": True, "data": _pipe(500, "failed")},
        ],
        "/pipelines/501": {"ok": True, "data": _pipe(501, "success")},
        "/jobs": {"ok": True, "data": []},
    }, calls)
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "ref": "main", "interval_s": 5})
    assert out["id"] == 500
    assert out["passed"] is False
    # …and it stopped re-resolving the ref after the first poll.
    assert sum(1 for c in calls if c[1].endswith("/pipelines")) == 1


def test_polling_does_not_re_download_the_logs_every_time(monkeypatch, _clock,
                                                          _attended):
    """A stage-1 failure with later stages still running re-fetched up to three
    job traces on EVERY poll and threw all but the last away — up to 200KB
    each, inside one tool call, to return 3KB."""
    calls: list = []
    _fake_request(monkeypatch, {
        "/pipelines/118": [
            {"ok": True, "data": _pipe(118, "running")},
            {"ok": True, "data": _pipe(118, "running")},
            {"ok": True, "data": _pipe(118, "failed")},
        ],
        "/jobs/4/trace": {"ok": True, "data": "boom"},
        "/jobs": {"ok": True, "data": [_job("test", "failed", jid=4)]},
    }, calls)
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 118, "interval_s": 5})
    traces = [c for c in calls if c[1].endswith("/trace")]
    assert len(traces) == 1, "the log was re-fetched while polling"
    assert out["logs"]["test"] == "boom"


# ── robustness ──────────────────────────────────────────────────────

def test_a_pipeline_id_cannot_escape_the_api_path(monkeypatch):
    """urllib.parse.quote defaults to safe="/", so a bare quote() leaves both
    `/` and `.` alone: a model-supplied id of ../../../../admin/ci/variables
    became a GET to an arbitrary path on the GitLab host, carrying the
    PRIVATE-TOKEN header. Nothing between the model and here coerces types."""
    calls: list = []
    _fake_request(monkeypatch, {"/pipelines": {"ok": True, "data": []}}, calls)
    out = gitlab.gitlab_pipeline(
        {"project": "g/p", "pipeline_id": "../../../../admin/ci/variables"})
    assert not out["ok"]
    assert "must be a number" in out["error"]
    assert not calls, "the traversal was sent to GitLab anyway"


def test_a_list_row_with_no_id_does_not_recurse_forever(monkeypatch):
    """It used to call itself with pipeline_id=None, fall back to the same list
    query, and recurse ~1000 HTTP calls deep before raising RecursionError
    through a module whose header promises it never raises into the loop."""
    _fake_request(monkeypatch, {"/pipelines": {"ok": True, "data": [{"ref": "main"}]}})
    out = gitlab.gitlab_pipeline({"project": "g/p", "ref": "main"})
    assert out["ok"] is False
    assert out["error"] == "unexpected_payload"


def test_a_non_dict_row_is_a_soft_error_not_an_exception(monkeypatch):
    _fake_request(monkeypatch, {"/pipelines": {"ok": True, "data": ["nope"]}})
    out = gitlab.gitlab_pipeline({"project": "g/p", "ref": "main"})
    assert out["ok"] is False
    assert out["error"] == "no_pipelines"


def test_a_long_log_returns_its_END_not_its_middle(monkeypatch):
    """The shared 200KB body cap is sized for issue bodies. Reading a capped
    prefix and then slicing `[-tail:]` returns the MIDDLE — so for
    npm/Gradle/Docker/pytest -v logs, the part that says why it failed was
    exactly the part dropped, silently."""
    calls: list = []
    log = ("noise\n" * 60_000) + "FATAL: the real reason\n"
    _fake_request(monkeypatch, {
        "/pipelines/119": {"ok": True, "data": _pipe(119, "failed")},
        "/trace": {"ok": True, "data": log},
        "/jobs": {"ok": True, "data": [_job("test", "failed", jid=5)]},
    }, calls)
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 119})
    assert "FATAL: the real reason" in out["logs"]["test"]
    assert out["logs"]["test [note]"].startswith("showing the last")
    # THE ACTUAL FIX: the trace is fetched with its own large cap. Asserting
    # `_TRACE_FETCH_CAP > _BODY_CAP` instead — as the first version did —
    # compares two constants and passes with the fix fully reverted.
    trace_call = [c for c in calls if c[1].endswith("/trace")][0]
    assert trace_call[3]["body_cap"] == gitlab._TRACE_FETCH_CAP


def test_a_log_bigger_than_the_fetch_cap_admits_it_is_not_the_end():
    """RAISING THE CAP IS NOT FIXING IT. http_request keeps the HEAD of an
    over-cap body, so above the cap `[-tail:]` is still the middle — and the
    first fix printed a confident "showing the last 3000 of 3000000 chars"
    over a slice that does not contain the failure. Wrong in the direction of
    reassurance is the worst direction."""
    import aiforge_core.runtime.tools.gitlab as g
    real = g._request

    def _req(method, path, params=None, body=None, **kw):
        if path.endswith("/trace"):
            # What http_request returns for a body that hit the cap.
            return {"ok": True, "data": "head of the log " * 100,
                    "body_cap_hit": kw.get("body_cap")}
        return real(method, path, params=params, body=body, **kw)
    g._request = _req
    try:
        txt, note = g._job_trace("g/p", 5, tail=50)
    finally:
        g._request = real
    assert "NOT THE END" in note
    assert str(g._TRACE_FETCH_CAP) in note
    assert txt.startswith("head of the log")   # the head, honestly labelled


def test_parse_json_false_returns_text_that_would_otherwise_be_parsed():
    """The whole point of the flag, at the layer that implements it. Every
    other test fakes `_request`, so they prove the flag is PASSED and never
    that http_request does anything with it — six mutations of this function
    survived the suite, including ignoring the flag entirely."""
    import urllib.request
    from aiforge_core.runtime.tools import _http_integration as H

    def _serve(body: bytes):
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return body[:n]
        return lambda *a, **k: _R()

    orig = urllib.request.urlopen
    try:
        for body in (b'{"error": "terraform exploded"}', b"   ", b""):
            urllib.request.urlopen = _serve(body)
            raw = H.http_request("GET", "https://gitlab.test/api", headers={},
                                 parse_json=False)
            assert raw["data"] == body.decode(), body
            # …and the default still parses, which is what every other caller
            # (jira, confluence, the gitlab JSON endpoints) depends on.
            urllib.request.urlopen = _serve(body)
            parsed = H.http_request("GET", "https://gitlab.test/api", headers={})
            if body.strip():
                assert parsed["data"] == {"error": "terraform exploded"}
            else:
                assert parsed["data"] == {}       # empty body -> {}
    finally:
        urllib.request.urlopen = orig


def test_a_non_json_body_is_still_returned_as_text_by_default():
    """The other half of the restructure: a body that is not JSON must come
    back as the text, not as {}."""
    import urllib.request
    from aiforge_core.runtime.tools import _http_integration as H

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return b"plain not json"[:n]
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _R()
    try:
        out = H.http_request("GET", "https://gitlab.test/api", headers={})
    finally:
        urllib.request.urlopen = orig
    assert out["data"] == "plain not json"
    assert "body_cap_hit" not in out       # not truncated -> key absent


def test_http_request_reports_truncation():
    """The evidence was already being read — `read(cap + 1)` — and thrown
    away, which is what made truncation undetectable downstream."""
    import io
    import urllib.request
    from aiforge_core.runtime.tools import _http_integration as H

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return b"x" * n
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _R()
    try:
        out = H.http_request("GET", "https://gitlab.test/api", headers={}, body_cap=100,
                             parse_json=False)
    finally:
        urllib.request.urlopen = orig
    # The CAP, named for what it holds — "truncated_bytes" reads as a count
    # of dropped bytes, which is a number nothing here knows.
    assert out["ok"]
    assert out["body_cap_hit"] == 100
    assert len(out["data"]) == 100


def test_ansi_and_section_markers_are_stripped(monkeypatch):
    """Both are pure noise and both eat the tail budget."""
    raw = ("section_start:1724500000:build\r\x1b[0K"
           "\x1b[31;1mERROR\x1b[0;m: it broke\n"
           "section_end:1724500009:build\r\x1b[0K")
    _fake_request(monkeypatch, {
        "/pipelines/120": {"ok": True, "data": _pipe(120, "failed")},
        "/trace": {"ok": True, "data": raw},
        "/jobs": {"ok": True, "data": [_job("t", "failed", jid=6)]},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 120})
    got = out["logs"]["t"]
    assert "ERROR: it broke" in got
    assert "\x1b" not in got
    assert "section_start" not in got


def test_an_unreadable_log_says_so_rather_than_vanishing(monkeypatch):
    """"It failed and there is no log" is itself the finding. Returning "" and
    skipping the key left failed_jobs populated with logs {} and no note."""
    _fake_request(monkeypatch, {
        "/pipelines/121": {"ok": True, "data": _pipe(121, "failed")},
        "/trace": {"ok": False, "error": "http 403"},
        "/jobs": {"ok": True, "data": [_job("t", "failed", jid=9)]},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 121})
    assert "http 403" in out["logs"]["t"]


def test_more_than_one_page_of_jobs_is_walked(monkeypatch):
    """GitLab caps per_page at 100. Silently taking page 1 of a matrix build
    shows `failed` with failed_jobs [] and no logs — the worst symptom there
    is, because it reads as "nothing failed"."""
    page1 = [_job(f"j{i}", "success", jid=i) for i in range(100)]
    page2 = [_job("late", "failed", jid=999)]
    pages = {"n": 0}

    def _req(method, path, params=None, body=None, **kw):
        if path.endswith("/jobs"):
            pages["n"] += 1
            return {"ok": True, "data": page1 if params["page"] == 1 else page2}
        if path.endswith("/trace"):
            return {"ok": True, "data": "the late one"}
        return {"ok": True, "data": _pipe(122, "failed")}
    monkeypatch.setattr(gitlab, "_request", _req)
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 122})
    assert out["failed_jobs"] == ["late"]
    assert len(out["jobs"]) == 101


def test_a_failed_pipeline_with_no_failed_job_names_the_child(monkeypatch):
    """The jobs endpoint does NOT return trigger (bridge) jobs, so a monorepo
    parent that failed purely because a triggered child failed reported
    failed_jobs [] with no logs — which reads as "nothing failed"."""
    _fake_request(monkeypatch, {
        "/pipelines/123": {"ok": True, "data": _pipe(123, "failed")},
        "/jobs": {"ok": True, "data": [_job("build")]},
        "/bridges": {"ok": True, "data": [
            {"name": "trigger-svc", "status": "failed", "allow_failure": False,
             "downstream_pipeline": {"id": 900, "status": "failed",
                                     "web_url": "https://gitlab.test/c/900"}}]},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 123})
    assert out["failed_jobs"] == []
    assert out["failed_child_pipelines"][0]["child_pipeline_id"] == 900
    assert "CHILD" in out["hint"]


def test_a_failed_pipeline_with_nothing_to_blame_admits_it(monkeypatch):
    _fake_request(monkeypatch, {
        "/pipelines/124": {"ok": True, "data": _pipe(124, "failed")},
        "/jobs": {"ok": True, "data": []},
        "/bridges": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 124})
    assert "no failed job was found" in out["hint"]


def test_logs_false_as_a_string_is_still_false(monkeypatch):
    """A local model sends {"logs": "false"}; bare truthiness reads that as
    yes and pays the round trips the caller declined."""
    calls: list = []
    _fake_request(monkeypatch, {
        "/pipelines/125": {"ok": True, "data": _pipe(125, "failed")},
        "/trace": {"ok": True, "data": "x"},
        "/jobs": {"ok": True, "data": [_job("t", "failed", jid=2)]},
    }, calls)
    out = gitlab.gitlab_pipeline(
        {"project": "g/p", "pipeline_id": 125, "logs": "false"})
    assert "logs" not in out
    assert not [c for c in calls if c[1].endswith("/trace")]


def test_the_newer_gitlab_statuses_are_known(monkeypatch):
    """`canceling` and `waiting_for_callback` are in the documented enum. The
    filter guard that exists to prevent a confusing 400 was refusing a query
    GitLab would have answered."""
    calls: list = []
    _fake_request(monkeypatch, {"/pipelines": {"ok": True, "data": []}}, calls)
    for st in ("canceling", "waiting_for_callback", "scheduled"):
        out = gitlab.gitlab_pipelines({"project": "g/p", "status": st})
        assert out["ok"], (st, out)


def test_the_full_sha_is_kept_alongside_the_short_one(monkeypatch):
    """The list endpoint's `sha` filter is an exact match on the full 40-char
    hash, so feeding the tool's own displayed sha back returned no_pipelines
    for a commit that has pipelines."""
    _fake_request(monkeypatch, {"/pipelines": {
        "ok": True, "data": [_pipe(126, "success")]}})
    row = gitlab.gitlab_pipelines({"project": "g/p"})["pipelines"][0]
    assert row["sha"] == "abcdef1234567890"
    assert row["sha_short"] == "abcdef123456"


def test_a_non_dict_downstream_pipeline_does_not_crash(monkeypatch):
    """`or {}` guards FALSY, not non-dict. A string here reached .get and
    raised AttributeError through a module whose header promises it never
    raises into the agent loop — and every other payload boundary in the file
    uses isinstance."""
    _fake_request(monkeypatch, {
        "/pipelines/127": {"ok": True, "data": _pipe(127, "failed")},
        "/jobs": {"ok": True, "data": []},
        "/bridges": {"ok": True, "data": [
            {"name": "t", "status": "failed", "downstream_pipeline": "oops"}]},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 127})
    assert out["ok"] is True
    assert out["failed_child_pipelines"][0]["child_pipeline_id"] is None


def test_a_partial_job_listing_is_never_presented_as_complete(monkeypatch):
    """Page 1 lands, page 2 blips. Returning 100 jobs with no error and no
    note presents a PARTIAL list as the whole one — so a pipeline whose only
    failing job was on page 2 reads as having nothing wrong with it."""
    page1 = [_job(f"j{i}", "success", jid=i) for i in range(100)]

    def _req(method, path, params=None, body=None, **kw):
        if path.endswith("/jobs"):
            if params["page"] == 1:
                return {"ok": True, "data": page1}
            return {"ok": False, "error": "http 500"}
        return {"ok": True, "data": _pipe(128, "failed")}
    monkeypatch.setattr(gitlab, "_request", _req)
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 128})
    assert out["failed_jobs"] == []
    assert "http 500" in out["jobs_truncated"]
    assert "may be missing" in out["jobs_truncated"]


def test_exactly_five_pages_does_not_claim_more_than(monkeypatch):
    """A 5th page of exactly 100 is indistinguishable from a 6th existing.
    Claiming "more than 500" for exactly 500 tells a user data is missing when
    it is not."""
    full = [_job(f"j{i}", "success", jid=i) for i in range(100)]

    def _req(method, path, params=None, body=None, **kw):
        if path.endswith("/jobs"):
            return {"ok": True, "data": full}
        return {"ok": True, "data": _pipe(129, "success")}
    monkeypatch.setattr(gitlab, "_request", _req)
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 129})
    assert len(out["jobs"]) == 500
    assert out["jobs_truncated"].startswith("at least 500")


def test_a_fatal_error_mid_watch_keeps_what_it_already_read(monkeypatch,
                                                            _clock, _attended):
    """A token rotated mid-watch. The docstring promises `passed` on every
    return that observed the pipeline at all — the fatal path had the data and
    dropped it."""
    _fake_request(monkeypatch, {
        "/pipelines/130": [
            {"ok": True, "data": _pipe(130, "running")},
            {"ok": False, "error": "http 403"},
        ],
        "/jobs": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 130, "interval_s": 5})
    assert out["ok"] is False
    assert out["error"] == "http 403"
    assert out["status"] == "running"     # …the snapshot survived
    assert out["passed"] is False
    assert out["stale"] is True


def test_a_pipeline_that_does_not_exist_yet_is_worth_waiting_for(
        monkeypatch, _clock, _attended):
    """"Push, then watch the branch" is the ADVERTISED use case, and GitLab
    routinely has not created the pipeline at the first poll (webhook lag,
    rules: evaluation, a mirrored repo). Classifying no_pipelines as fatal
    killed the watch on check 1 for exactly the thing it was built for."""
    _fake_request(monkeypatch, {
        "/pipelines": [
            {"ok": True, "data": []},
            {"ok": True, "data": [_pipe(131, "success")]},
        ],
        "/pipelines/131": {"ok": True, "data": _pipe(131, "success")},
        "/jobs": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "ref": "main", "interval_s": 5})
    assert out["ok"]
    assert out["passed"]
    assert out["checks"] == 2


def test_a_failed_terminal_reread_does_not_look_like_no_log(monkeypatch,
                                                            _clock, _attended):
    """A 429 right at completion is common. Falling back silently to the
    logs=False snapshot hands back a clean, finished, failed result naming a
    job with no log and no reason there is no log."""
    _fake_request(monkeypatch, {
        "/pipelines/132": [
            {"ok": True, "data": _pipe(132, "failed")},
            {"ok": False, "error": "http 429"},
        ],
        "/jobs": {"ok": True, "data": [_job("t", "failed", jid=3)]},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 132, "interval_s": 5})
    assert out["logs_error"] == "http 429"
    assert "read it again" in out["hint"]


def test_polling_does_not_walk_the_job_pages(monkeypatch, _clock, _attended):
    """Only `finished` decides whether to keep polling. Walking up to five
    100-job pages per poll put a bigger cost back in the place the log
    re-download fix had just emptied."""
    calls: list = []
    _fake_request(monkeypatch, {
        "/pipelines/133": [
            {"ok": True, "data": _pipe(133, "running")},
            {"ok": True, "data": _pipe(133, "running")},
            {"ok": True, "data": _pipe(133, "success")},
        ],
        "/jobs": {"ok": True, "data": []},
    }, calls)
    gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 133, "interval_s": 5})
    job_calls = [c for c in calls if c[1].endswith("/jobs")]
    assert len(job_calls) == 1, "job pages were walked while polling"


def test_a_stopped_watch_carries_the_same_envelope_as_every_other_return(
        monkeypatch, _clock):
    _fake_request(monkeypatch, {
        "/pipelines/134": {"ok": True, "data": _pipe(134, "running")},
        "/jobs": {"ok": True, "data": []},
    })
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "active", lambda: "s")
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: True)
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "pipeline_id": 134, "interval_s": 5})
    for key in ("checks", "elapsed_s"):
        assert key in out, key
    # …and no `requests`: it only ever equalled `checks` or `checks + 1`, so
    # it carried nothing while its name implied HTTP volume (1-20x higher).
    assert "requests" not in out


def test_an_empty_string_is_unset_not_no(monkeypatch):
    """`{"logs": ""}` from a model means "I did not specify", not "no" — and
    reading it as no silently loses the whole point of the tool."""
    _fake_request(monkeypatch, {
        "/pipelines/135": {"ok": True, "data": _pipe(135, "failed")},
        "/trace": {"ok": True, "data": "why"},
        "/jobs": {"ok": True, "data": [_job("t", "failed", jid=2)]},
    })
    out = gitlab.gitlab_pipeline(
        {"project": "g/p", "pipeline_id": 135, "logs": ""})
    assert out["logs"]["t"] == "why"


def test_an_id_arriving_as_a_json_float_still_works():
    """JSON has one number type, so an id can reach the tool as 12.0 through
    the very ARGS_JSON path the numeric guard exists to defend."""
    assert gitlab._enc_id(12.0) == ("12", None)
    assert gitlab._enc_id(True)[1]          # a bool is not an id
    assert gitlab._enc_id("١٢٣")[1]         # isdigit() is True for these


def test_the_issue_and_mr_paths_cannot_traverse_either(monkeypatch):
    """The SAME hole, on the WRITE verbs — gitlab_comment and gitlab_update
    POST to a path built from a model-supplied iid.

    A HARD ERROR, not a quiet escape-and-send: escaping stops the traversal,
    but these are approval-gated writes, so sending anyway burns a human
    Approve and an authenticated POST to earn a 404 — from which the model
    concludes "that issue doesn't exist" rather than "your iid was
    malformed"."""
    calls: list = []
    _fake_request(monkeypatch, {"": {"ok": True, "data": {}}}, calls)
    for fn, kw in ((gitlab.gitlab_comment, {"body": "x"}),
                   (gitlab.gitlab_mr_comment, {"body": "x"}),
                   (gitlab.gitlab_update, {"title": "x"}),
                   (gitlab.gitlab_read, {})):
        out = fn({"project": "g/p", "iid": "../../../../users/1", **kw})
        assert out["ok"] is False, fn.__name__
        assert "bad iid" in out["error"], (fn.__name__, out)
    assert not calls, "a malformed iid still cost a request"


def test_a_child_failure_is_reported_even_when_a_job_also_failed(monkeypatch):
    """A parent can fail for both reasons. Only looking at bridges when nothing
    else failed hid the child in exactly the messier case — and reverting that
    left all 53 earlier tests green, so it was fixed but not pinned."""
    _fake_request(monkeypatch, {
        "/pipelines/136": {"ok": True, "data": _pipe(136, "failed")},
        "/trace": {"ok": True, "data": "boom"},
        "/jobs": {"ok": True, "data": [_job("t", "failed", jid=2)]},
        "/bridges": {"ok": True, "data": [
            {"name": "svc", "status": "failed", "allow_failure": False,
             "downstream_pipeline": {"id": 901, "status": "failed"}}]},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 136})
    assert out["failed_jobs"] == ["t"]
    assert out["failed_child_pipelines"][0]["child_pipeline_id"] == 901


def test_a_cancelled_child_counts_as_a_failure(monkeypatch):
    """GitLab spells it `canceled`, a bridge can read `canceling`, and this
    file translates `cancelled` elsewhere. Matching only "failed" silently
    dropped the child."""
    for spelling in ("canceled", "cancelled", "canceling"):
        _fake_request(monkeypatch, {
            "/pipelines/137": {"ok": True, "data": _pipe(137, "failed")},
            "/jobs": {"ok": True, "data": []},
            "/bridges": {"ok": True, "data": [
                {"name": "svc", "status": spelling, "allow_failure": False,
                 "downstream_pipeline": {"id": 902}}]},
        })
        out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 137})
        assert out.get("failed_child_pipelines"), spelling


def test_a_jobs_error_on_a_FAILED_pipeline_still_explains_itself(monkeypatch):
    """Returning early on a jobs error skipped the bridge check and the
    "no failed job found" hint — leaving `status: failed` with no explanation
    at all, which is the symptom this module argues against, not the fix. The
    existing test only covered a SUCCESS pipeline."""
    _fake_request(monkeypatch, {
        "/pipelines/138": {"ok": True, "data": _pipe(138, "failed")},
        "/jobs": {"ok": False, "error": "http 403"},
        "/bridges": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 138})
    assert out["jobs_error"] == "http 403"
    # …naming the error it KNOWS. The first version of this test asserted the
    # generic "no failed job was found" wording, pinning a hint that offers
    # three speculative causes while the real one sits two keys above it.
    assert "http 403" in out["hint"]


def test_a_pipeline_object_with_no_id_cannot_unpin_a_ref_watch(
        monkeypatch, _clock, _attended):
    """A 200-OK error body from a proxy/WAF summarises to all-None. Without the
    same guard the LIST row has, the watch never pins an id, re-resolves
    "latest on this ref" every poll, and reports a colleague's later push as
    the user's result."""
    _fake_request(monkeypatch, {
        "/pipelines": {"ok": True, "data": [_pipe(500, "running")]},
        "/pipelines/500": {"ok": True, "data": {"message": "403 Forbidden"}},
        "/jobs": {"ok": True, "data": []},
    })
    out = gitlab.gitlab_pipeline_watch(
        {"project": "g/p", "ref": "main", "interval_s": 5})
    assert out["ok"] is False
    assert out["error"] == "unexpected_payload"


def test_a_polling_snapshot_never_looks_like_a_complete_one(monkeypatch,
                                                            _clock, _attended):
    """The job-skip is a PARAMETER, not a key in `args`, and it announces
    itself when used.

    As a key it was model-injectable: the loop hands raw parsed args to the
    tool, the schema allows additional properties, and the wrapper passes them
    through — so a prompt-injected `"_skip_jobs": true` produced a failed
    pipeline with no failed_jobs, no logs, no bridge check and no hint. The
    comment claiming the model could not set it was simply false."""
    _fake_request(monkeypatch, {
        "/pipelines/139": {"ok": True, "data": _pipe(139, "running")},
        "/jobs": {"ok": True, "data": []},
    })
    snap = gitlab.gitlab_pipeline(
        {"project": "g/p", "pipeline_id": 139}, skip_jobs=True)
    assert "jobs" not in snap
    assert snap["jobs_omitted"]
    # …and NEITHER spelling reaches it from the args a model controls.
    for injected in ("_skip_jobs", "jobs"):
        full = gitlab.gitlab_pipeline(
            {"project": "g/p", "pipeline_id": 139, injected: True})
        assert "jobs" in full, injected
        assert "jobs_omitted" not in full, injected


def test_the_tool_dispatcher_cannot_be_talked_into_skipping_the_jobs(monkeypatch):
    """End to end through the registry, the way an injected arg would arrive."""
    import json
    from aiforge_core.runtime.chat_agent._registry import TOOLS
    _fake_request(monkeypatch, {
        "/pipelines/143": {"ok": True, "data": _pipe(143, "failed")},
        "/jobs": {"ok": True, "data": [_job("t", "failed", jid=1)]},
        "/trace": {"ok": True, "data": "why"},
        "/bridges": {"ok": True, "data": []},
    })
    args = json.loads('{"project": "g/p", "pipeline_id": 143, '
                      '"_skip_jobs": true, "jobs": false}')
    out = TOOLS["gitlab_pipeline"](args, ".")
    assert out["failed_jobs"] == ["t"]
    assert out["logs"]["t"] == "why"


def test_a_json_shaped_job_log_is_still_a_log(monkeypatch):
    """The trace endpoint is plain TEXT pulled through a JSON-parsing helper:
    a job whose output IS json (terraform show -json, kubectl -o json, any
    JSON logger) came back as a dict and the log was discarded as an
    "unexpected payload"."""
    calls: list = []
    _fake_request(monkeypatch, {
        "/pipelines/141": {"ok": True, "data": _pipe(141, "failed")},
        "/trace": {"ok": True, "data": '{"error": "terraform exploded"}'},
        "/jobs": {"ok": True, "data": [_job("plan", "failed", jid=4)]},
    }, calls)
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 141})
    assert "terraform exploded" in out["logs"]["plan"]
    trace_call = [c for c in calls if c[1].endswith("/trace")][0]
    assert trace_call[3]["parse_json"] is False


def test_an_empty_job_log_says_empty_not_unexpected(monkeypatch):
    """A failed job whose trace has not flushed yet — common at the exact
    moment the watch's terminal re-read fires. "unexpected payload" points the
    reader at a bug that isn't there."""
    _fake_request(monkeypatch, {
        "/pipelines/142": {"ok": True, "data": _pipe(142, "failed")},
        "/trace": {"ok": True, "data": "   "},
        "/jobs": {"ok": True, "data": [_job("t", "failed", jid=4)]},
    })
    out = gitlab.gitlab_pipeline({"project": "g/p", "pipeline_id": 142})
    assert "empty" in out["logs"]["t"]


# ── registration ────────────────────────────────────────────────────

def test_the_tools_are_reachable_by_the_agent():
    """A tool nobody registered is a tool that does not exist. Every layer:
    the registry the loop dispatches through, the schema catalogue the model
    reads, and the prompt list it picks names from."""
    from aiforge_core.runtime.chat_agent._registry import (TOOLS,
                                                           _READONLY_TOOLS)
    from aiforge_core.runtime.tools.tool_policy import _READONLY_ALWAYS_ALLOW
    from aiforge_core.runtime.chat_agent._tools._schemas import CATALOG
    from aiforge_core.runtime.chat_agent import _prompt
    for name in ("gitlab_pipelines", "gitlab_pipeline", "gitlab_pipeline_watch"):
        assert name in TOOLS, f"{name} is not dispatchable"
        assert name in CATALOG, f"{name} has no schema for the model to read"
        # A SEPARATE classification a read-only tool does not join by being
        # read-only. docs/TOOLS.md marks these RO and Plan mode refused them
        # anyway — the exact drift the comment above that list describes.
        assert name in _READONLY_TOOLS, f"{name} is blocked in Plan mode"
        # The OTHER read-only classification. _registry's own comment says the
        # two must stay in sync; fixing only the one Plan mode reads leaves
        # these unpinned against any future tightening of the policy default.
        assert name in _READONLY_ALWAYS_ALLOW, f"{name} not pinned to ALLOW"
    src = (_prompt.__file__ and open(_prompt.__file__).read()) or ""
    for name in ("gitlab_pipelines", "gitlab_pipeline", "gitlab_pipeline_watch"):
        # The full catalogue line, not a substring: `"gitlab_pipeline" in src`
        # is satisfied by the PLURAL, so the singular could be missing and the
        # assertion would still pass.
        assert f"- {name}{{{{" in src, f"{name} is missing from the prompt catalogue"


def test_the_watch_tool_is_not_a_shell_command_carrier():
    """The approval gate is keyed on the TOOL NAME: any new tool that carries a
    shell command under `cmd` bypasses risk assessment and every PreToolUse
    hook. These carry no command — assert that stays true."""
    from aiforge_core.runtime.chat_agent._tools._schemas import CATALOG
    for name in ("gitlab_pipelines", "gitlab_pipeline", "gitlab_pipeline_watch"):
        assert "cmd" not in CATALOG[name][1]
