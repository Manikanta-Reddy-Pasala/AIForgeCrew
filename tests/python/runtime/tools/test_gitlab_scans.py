"""gitlab_pipeline: recognising the Sonar and Black Duck jobs from their
logs, whatever the jobs are named, and attaching what the scan found.

GitLab is faked at ``gitlab._request``; the two scan tools at their report
functions, so what is under test is the recognition and the wiring.
"""
import pytest

from aiforge_core.runtime.tools import _gitlab_scans as scans
from aiforge_core.runtime.tools import blackduck, gitlab, sonar

SONAR = "https://sonar.test"
BD = "https://bd.test"
BD_LINK = (BD + "/api/projects/11111111-2222-3333-4444-555555555555"
           "/versions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/components")

SONAR_LOG = f"""\
$ sonar-scanner -Dsonar.projectKey=$CI_PROJECT_NAME
INFO: Scanner configuration file: /opt/sonar-scanner/conf/sonar-scanner.properties
INFO: Project key: shop
INFO: Pull request 12 for merge into main from feat/cart
INFO: ANALYSIS SUCCESSFUL, you can find the results at: {SONAR}/dashboard?id=shop&pullRequest=12
INFO: More about the report processing at {SONAR}/api/ce/task?id=AYx-1
ERROR: QUALITY GATE STATUS: FAILED - View details on {SONAR}/dashboard?id=shop&pullRequest=12
ERROR: Job failed: exit code 3
"""
BD_LOG = f"""\
$ bash <(curl -s -L https://detect.synopsys.com/detect9.sh) --detect.project.name=$CI_PROJECT_NAME
2026-10-13 09:00:01 INFO  [main] --- Project name: shop
2026-10-13 09:00:01 INFO  [main] --- Project version: main
2026-10-13 09:03:10 INFO  [main] --- Black Duck Project BOM: {BD_LINK}
2026-10-13 09:03:40 ERROR [main] --- Policy Severity: BLOCKER
2026-10-13 09:03:41 INFO  [main] --- Overall Status: FAILURE_POLICY_VIOLATION - violations found.
ERROR: Job failed: exit code 3
"""
PLAIN_LOG = "$ pytest -q\n412 passed in 31.2s\nJob succeeded\n"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.test")
    monkeypatch.setenv("GITLAB_TOKEN", "t0ken")
    monkeypatch.delenv("GITLAB_PROJECT", raising=False)
    for var in ("SONAR_BASE_URL", "SONAR_TOKEN", "BLACKDUCK_BASE_URL",
                "BLACKDUCK_TOKEN", "GITLAB_SCAN_PROBE_JOBS"):
        monkeypatch.delenv(var, raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    scans._seen.clear()
    yield
    scans._seen.clear()


def _both(monkeypatch):
    monkeypatch.setenv("SONAR_BASE_URL", SONAR)
    monkeypatch.setenv("SONAR_TOKEN", "s")
    monkeypatch.setenv("BLACKDUCK_BASE_URL", BD)
    monkeypatch.setenv("BLACKDUCK_TOKEN", "b")


def _job(jid, name, status="success", stage="test", **kw):
    return {"id": jid, "name": name, "stage": stage, "status": status,
            "allow_failure": False, **kw}


def _gitlab(monkeypatch, status, jobs, logs):
    """A pipeline with ``jobs``; ``logs`` maps job id → trace text. Returns the
    list of job ids whose trace was fetched."""
    fetched = []

    def _req(method, path, params=None, body=None, **kw):
        if path.endswith("/trace"):
            jid = int(path.split("/")[-2])
            fetched.append(jid)
            return {"ok": True, "data": logs.get(jid, PLAIN_LOG)}
        if path.endswith("/jobs"):
            return {"ok": True, "data": jobs}
        if path.endswith("/bridges"):
            return {"ok": True, "data": []}
        pipe = {"id": 101, "status": status, "ref": "main", "sha": "abc"}
        if path.endswith("/pipelines"):
            return {"ok": True, "data": [pipe]}
        if path.endswith("/pipelines/101"):
            return {"ok": True, "data": pipe}
        return {"ok": False, "error": "http 404"}
    monkeypatch.setattr(gitlab, "_request", _req)
    return fetched


def _reports(monkeypatch):
    """Fake both scan tools; returns the args each was called with."""
    asked = {"sonar": [], "blackduck": []}

    def _sonar(args, cwd=None):
        asked["sonar"].append(args)
        return {"ok": True, "project": "shop",
                "quality_gate": {"status": "ERROR", "passed": False}}

    def _bd(args, cwd=None):
        asked["blackduck"].append(args)
        return {"ok": True, "project": "shop",
                "policy": {"status": "IN_VIOLATION", "in_violation": True}}
    monkeypatch.setattr(sonar, "sonar_report", _sonar)
    monkeypatch.setattr(blackduck, "blackduck_report", _bd)
    return asked


# ── recognising a scanner in a log ──────────────────────────────────

def test_a_sonar_log_gives_the_task_link_and_the_names():
    found = scans.find_sonar(SONAR_LOG)
    assert found["link"] == SONAR + "/api/ce/task?id=AYx-1"
    # `$CI_PROJECT_NAME` is the command as echoed; "shop" is what ran.
    assert found["project"] == "shop"
    assert found["pull_request"] == "12"
    assert found["log_verdict"] == "FAILED"


def test_a_blackduck_log_gives_the_bom_link_and_the_names():
    found = scans.find_blackduck(BD_LOG)
    assert found["link"] == BD_LINK
    assert found["project"] == "shop" and found["version"] == "main"
    assert found["log_verdict"] == "FAILURE_POLICY_VIOLATION"


def test_an_ordinary_log_is_no_scan():
    assert scans.find_scans(PLAIN_LOG) == []
    assert scans.find_scans("") == []


def test_one_log_can_hold_both_scanners():
    assert [f["tool"] for f in scans.find_scans(SONAR_LOG + BD_LOG)] == [
        "sonar", "blackduck"]


def test_a_link_at_the_end_of_a_sentence_loses_the_full_stop():
    found = scans.find_sonar(
        f"Results are at {SONAR}/dashboard?id=shop&branch=main.\n")
    assert found["link"] == SONAR + "/dashboard?id=shop&branch=main"
    assert found["branch"] == "main"


def test_a_retried_scanner_counts_by_its_last_run():
    again = SONAR_LOG.replace("AYx-1", "AYx-2").replace("FAILED", "PASSED")
    found = scans.find_sonar(SONAR_LOG + again)
    assert found["link"].endswith("id=AYx-2")
    assert found["log_verdict"] == "PASSED"


def test_a_masked_host_still_gives_the_project_and_pull_request():
    log = ("INFO: ANALYSIS SUCCESSFUL, you can find the results at: "
           "[MASKED]/dashboard?id=shop&pullRequest=12\n")
    found = scans.find_sonar(log)
    assert found["link"] == "" and found["ran"] is True
    assert found["project"] == "shop" and found["pull_request"] == "12"


def test_an_echoed_dashboard_link_is_not_a_scan_that_ran():
    found = scans.find_sonar(
        '$ sonar-scanner && echo "$SONAR_HOST_URL/dashboard?id=$CI_PROJECT_NAME"\n')
    assert found["ran"] is False and found["project"] == ""


@pytest.mark.parametrize("line", [
    "[INFO] --- sonar-maven-plugin:3.9.1.2184:sonar (default-cli) @ shop ---",
    "$ mvn -B verify sonar:sonar",
    "> Task :sonarqube",
])
def test_build_tool_sonar_runs_are_recognised(line):
    found = scans.find_sonar(line + "\nBUILD FAILURE\n")
    assert found["tool"] == "sonar" and found["ran"] is False


@pytest.mark.parametrize("line", [
    "$ ./ci/autodetect.sh --all",
    "Downloaded from central: https://repo/com/synopsys/integration/"
    "blackduck-common/66.2.1/blackduck-common-66.2.1.jar",
    "Project name: shop",
    "secret_detection finished",
])
def test_lookalike_lines_are_not_a_blackduck_scan(line):
    assert scans.find_blackduck(line + "\n") == {}


def test_a_detect_script_is_a_blackduck_scan():
    assert scans.find_blackduck("$ bash detect9.sh --x\n")["tool"] == "blackduck"


def test_an_unexpanded_variable_is_not_a_project_name():
    found = scans.find_sonar("$ sonar-scanner -Dsonar.projectKey=$KEY\n")
    assert found["project"] == ""
    # Half a name is not a name either.
    found = scans.find_sonar(
        "$ sonar-scanner -Dsonar.projectKey=shop_$CI_COMMIT_REF_SLUG\n")
    assert found["project"] == ""
    found = scans.find_blackduck("Black Duck\nProject name: $CI_PROJECT_NAME\n")
    assert found["project"] == ""


def test_a_hostile_log_is_searched_in_bounded_time():
    """One whitespace-free line of URL starts, and one line of padding after a
    name: both were quadratic for a pattern run over the whole log."""
    import time
    urls = "http://" * 400_000
    pad = "Black Duck\nProject name: x" + " " * 1_500_000 + "y\n"
    start = time.monotonic()
    scans.find_scans(urls + "/dashboard?id=x /api/ce/task?id=1 /versions/\n")
    scans.find_scans(pad)
    assert time.monotonic() - start < 2.0


def test_probe_order_names_the_scanner_first_then_checks_then_the_rest():
    jobs = [_job(1, "build"), _job(2, "secret_detection"),
            _job(3, "container_scanning"), _job(4, "unit"),
            _job(5, "sonarqube-check"), _job(6, "deploy"), _job(7, "bd"),
            _job(8, "x", stage="security"), _job(9, "late"), _job(10, "later")]
    order = [j["id"] for j in scans.jobs_to_probe(jobs, already={4})]
    # The two that name a scanner, the two that sound like a check, then the
    # latest of the rest — six in all by default. `secret_detection` neither
    # names a scanner nor is skipped: it waits its turn with the rest.
    assert order == [7, 5, 8, 3, 10, 9]


# ── inside gitlab_pipeline ──────────────────────────────────────────

def test_a_failed_scan_job_brings_its_result_whatever_it_is_called(monkeypatch):
    _both(monkeypatch)
    asked = _reports(monkeypatch)
    fetched = _gitlab(monkeypatch, "failed",
                      [_job(1, "build"), _job(2, "verify-7", "failed")],
                      {2: SONAR_LOG + BD_LOG})
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    assert out["failed_jobs"] == ["verify-7"]
    by_tool = {s["tool"]: s for s in out["scans"]}
    assert by_tool["sonar"]["job"] == "verify-7"
    assert by_tool["sonar"]["job_status"] == "failed"
    assert by_tool["sonar"]["log_verdict"] == "FAILED"
    assert by_tool["sonar"]["result"]["quality_gate"]["passed"] is False
    assert "ok" not in by_tool["sonar"]["result"]
    assert by_tool["blackduck"]["result"]["policy"]["in_violation"] is True
    assert asked["sonar"] == [{"limit": 10,
                               "url": SONAR + "/api/ce/task?id=AYx-1"}]
    assert asked["blackduck"] == [{"limit": 10, "url": BD_LINK}]
    # Both found in the log already in hand: no other job's log was fetched.
    assert fetched == [2]
    # The scans sit before the job list, which is what gets cut when long.
    keys = list(out)
    assert keys.index("scans") < keys.index("jobs")


def test_a_passing_pipeline_is_searched_for_its_scan_jobs(monkeypatch):
    _both(monkeypatch)
    asked = _reports(monkeypatch)
    fetched = _gitlab(monkeypatch, "success",
                      [_job(1, "build", stage="build"), _job(2, "unit"),
                       _job(3, "quality"), _job(4, "release-notes", stage="post"),
                       _job(5, "x9", stage="post")],
                      {3: SONAR_LOG, 5: BD_LOG})
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    assert out["passed"] is True
    assert {s["tool"]: s["job"] for s in out["scans"]} == {
        "sonar": "quality", "blackduck": "x9"}
    assert len(asked["sonar"]) == 1 and len(asked["blackduck"]) == 1
    # The job whose name suggests a scan is read first; the rest newest first,
    # and reading stops once both scans have a link.
    assert fetched == [3, 5]


def test_nothing_configured_means_no_extra_log_is_fetched(monkeypatch):
    fetched = _gitlab(monkeypatch, "success",
                      [_job(1, "build"), _job(2, "sonar")], {2: SONAR_LOG})
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    assert fetched == [] and "scans" not in out


def test_an_unconfigured_scanner_in_a_failed_log_is_still_named(monkeypatch):
    asked = _reports(monkeypatch)
    _gitlab(monkeypatch, "failed", [_job(2, "sonar", "failed")], {2: SONAR_LOG})
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    (scan,) = out["scans"]
    assert scan["tool"] == "sonar" and "result" not in scan
    assert scan["link"] == SONAR + "/api/ce/task?id=AYx-1"
    assert "not set up" in scan["note"]
    assert asked["sonar"] == []


def test_scans_false_skips_the_search(monkeypatch):
    _both(monkeypatch)
    _reports(monkeypatch)
    fetched = _gitlab(monkeypatch, "success", [_job(3, "sonar")], {3: SONAR_LOG})
    out = gitlab.gitlab_pipeline({"project": "g/p", "scans": False})
    assert fetched == [] and "scans" not in out


def test_the_log_with_the_link_beats_one_that_only_mentions_the_scanner(monkeypatch):
    """A build job that writes sonar.projectKey into a properties file is not
    the scan. The job that printed the result link is."""
    monkeypatch.setenv("SONAR_BASE_URL", SONAR)
    monkeypatch.setenv("SONAR_TOKEN", "s")
    asked = _reports(monkeypatch)
    _gitlab(monkeypatch, "failed",
            [_job(1, "build", "failed"), _job(2, "code-check")],
            {1: "echo sonar.projectKey=shop > sonar-project.properties\nboom\n",
             2: SONAR_LOG})
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    (scan,) = out["scans"]
    assert scan["job"] == "code-check"
    assert asked["sonar"] == [{"limit": 10,
                               "url": SONAR + "/api/ce/task?id=AYx-1"}]


def test_no_link_falls_back_to_the_project_the_scanner_named(monkeypatch):
    _both(monkeypatch)
    asked = _reports(monkeypatch)
    log = ("Black Duck\nProject name: shop\nProject version: feat-x\n"
           "Overall Status: FAILURE_BLACKDUCK_CONNECTIVITY\n")
    _gitlab(monkeypatch, "failed", [_job(2, "sca", "failed")], {2: log})
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    assert asked["blackduck"] == [{"limit": 10, "project": "shop",
                                   "version": "feat-x"}]
    assert out["scans"][0]["log_verdict"] == "FAILURE_BLACKDUCK_CONNECTIVITY"
    # The scan never reached the server: what comes back is an older result.
    assert "may be older than this pipeline" in out["scans"][0]["note"]


def test_a_link_on_another_server_is_never_opened(monkeypatch):
    """CI often reaches the scan server by another name than the one set up
    here. That link is not opened; the project the log named is looked up on
    the configured server instead, and the answer says so."""
    _both(monkeypatch)
    asked = _reports(monkeypatch)
    log = SONAR_LOG.replace(SONAR, "http://sonarqube:9000")
    _gitlab(monkeypatch, "failed", [_job(2, "sonar", "failed")], {2: log})
    (scan,) = gitlab.gitlab_pipeline({"project": "g/p"})["scans"]
    assert asked["sonar"] == [{"limit": 10, "project": "shop", "branch": "",
                               "pull_request": "12"}]
    assert "another host" in scan["note"] and "LATEST result" in scan["note"]
    assert scan["result"]["quality_gate"]["passed"] is False


def test_a_foreign_link_with_no_name_is_reported_not_opened(monkeypatch):
    _both(monkeypatch)
    asked = _reports(monkeypatch)
    log = f"Black Duck Project BOM: {BD_LINK.replace(BD, 'https://other.test')}\n"
    _gitlab(monkeypatch, "failed", [_job(2, "oss", "failed")], {2: log})
    (scan,) = gitlab.gitlab_pipeline({"project": "g/p"})["scans"]
    assert asked["blackduck"] == [] and "result" not in scan
    assert "not on the configured Black Duck server" in scan["note"]


def test_a_task_link_the_token_may_not_read_falls_back_to_the_project(monkeypatch):
    _both(monkeypatch)
    asked = []

    def _sonar(args, cwd=None):
        asked.append(args)
        if "url" in args:
            return {"ok": False, "error": "http 403", "hint": "task link"}
        return {"ok": True, "project": "shop",
                "quality_gate": {"status": "OK", "passed": True}}
    monkeypatch.setattr(sonar, "sonar_report", _sonar)
    _gitlab(monkeypatch, "failed", [_job(2, "sonar", "failed")], {2: SONAR_LOG})
    (scan,) = gitlab.gitlab_pipeline({"project": "g/p"})["scans"]
    assert [sorted(a) for a in asked] == [
        ["limit", "url"], ["branch", "limit", "project", "pull_request"]]
    assert asked[1]["pull_request"] == "12"
    assert scan["result"]["quality_gate"]["passed"] is True
    assert "LATEST result" in scan["note"]


def test_a_result_read_by_name_says_it_may_not_be_this_run(monkeypatch):
    _both(monkeypatch)
    _reports(monkeypatch)
    log = "$ mvn sonar:sonar\nINFO: Project key: shop\nBUILD FAILURE\n"
    _gitlab(monkeypatch, "failed", [_job(2, "verify", "failed")], {2: log})
    (scan,) = gitlab.gitlab_pipeline({"project": "g/p"})["scans"]
    assert "may be older than this pipeline" in scan["note"]


def test_between_two_logs_that_both_ran_the_first_read_wins(monkeypatch):
    _both(monkeypatch)
    asked = _reports(monkeypatch)
    _gitlab(monkeypatch, "failed",
            [_job(2, "sonar-a", "failed"), _job(3, "sonar-b", "failed")],
            {2: SONAR_LOG + BD_LOG, 3: SONAR_LOG.replace("AYx-1", "AYx-9")})
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    by_tool = {s["tool"]: s for s in out["scans"]}
    assert by_tool["sonar"]["job"] == by_tool["blackduck"]["job"] == "sonar-a"
    assert asked["sonar"][0]["url"].endswith("AYx-1")
    # A second scan seen in a log that was read is named, not silently lost.
    # (The search itself stops at the first one: it does not read on.)
    assert by_tool["sonar"]["also_ran_in_jobs"] == ["sonar-b"]
    assert "also_ran_in_jobs" not in by_tool["blackduck"]


def test_a_finished_jobs_log_is_not_fetched_twice(monkeypatch):
    _both(monkeypatch)
    _reports(monkeypatch)
    fetched = _gitlab(monkeypatch, "success",
                      [_job(3, "sonar"), _job(5, "bd")],
                      {3: SONAR_LOG, 5: BD_LOG})
    first = gitlab.gitlab_pipeline({"project": "g/p"})
    second = gitlab.gitlab_pipeline({"project": "g/p"})
    assert sorted(fetched) == [3, 5]
    assert [s["tool"] for s in second["scans"]] == \
        [s["tool"] for s in first["scans"]] == ["blackduck", "sonar"]


def test_a_log_with_no_scan_in_it_is_read_again_next_time(monkeypatch):
    """Read just after the job ends, a log can be half flushed."""
    _both(monkeypatch)
    _reports(monkeypatch)
    logs = {3: "$ sonar-scanner\n"}
    fetched = _gitlab(monkeypatch, "success", [_job(3, "sonar")], logs)
    gitlab.gitlab_pipeline({"project": "g/p"})
    logs[3] = SONAR_LOG
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    assert fetched == [3, 3]
    assert out["scans"][0]["result"]["quality_gate"]["passed"] is False


def test_logs_false_still_finds_the_failed_scan_job(monkeypatch):
    _both(monkeypatch)
    _reports(monkeypatch)
    _gitlab(monkeypatch, "failed", [_job(2, "gate", "failed")],
            {2: SONAR_LOG + BD_LOG})
    out = gitlab.gitlab_pipeline({"project": "g/p", "logs": False})
    assert "logs" not in out
    assert [s["tool"] for s in out["scans"]] == ["blackduck", "sonar"]


def test_a_scan_server_error_is_carried_with_its_hint(monkeypatch):
    _both(monkeypatch)
    monkeypatch.setattr(sonar, "sonar_report", lambda args, cwd=None: {
        "ok": False, "error": "http 403", "hint": "no Browse permission"})
    _gitlab(monkeypatch, "failed", [_job(2, "sonar", "failed")], {2: SONAR_LOG})
    (scan,) = gitlab.gitlab_pipeline({"project": "g/p"})["scans"]
    assert scan["error"] == "http 403" and scan["hint"] == "no Browse permission"
    assert "result" not in scan


def test_a_scan_reader_that_raises_costs_only_its_own_entry(monkeypatch):
    _both(monkeypatch)
    _reports(monkeypatch)

    def _boom(args, cwd=None):
        raise RuntimeError("scan server exploded")
    monkeypatch.setattr(sonar, "sonar_report", _boom)
    _gitlab(monkeypatch, "failed", [_job(2, "sonar", "failed")],
            {2: SONAR_LOG + BD_LOG})
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    assert out["ok"] and out["failed_jobs"] == ["sonar"]
    by_tool = {s["tool"]: s for s in out["scans"]}
    assert "exploded" in by_tool["sonar"]["error"]
    assert by_tool["blackduck"]["result"]["policy"]["in_violation"] is True
    assert "QUALITY GATE STATUS" in out["logs"]["sonar"]


def test_a_broken_search_does_not_cost_the_pipeline_answer(monkeypatch):
    _both(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("search exploded")
    monkeypatch.setattr(scans, "collect", _boom)
    _gitlab(monkeypatch, "failed", [_job(2, "sonar", "failed")], {2: SONAR_LOG})
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    assert out["ok"] and out["failed_jobs"] == ["sonar"]
    assert "exploded" in out["scans_error"] and "scans" not in out


def test_the_search_stops_at_its_budget(monkeypatch):
    _both(monkeypatch)
    _reports(monkeypatch)
    monkeypatch.setenv("GITLAB_SCAN_PROBE_JOBS", "2")
    jobs = [_job(n, f"job{n}") for n in range(1, 9)]
    fetched = _gitlab(monkeypatch, "success", jobs, {})
    out = gitlab.gitlab_pipeline({"project": "g/p"})
    assert len(fetched) == 2 and "scans" not in out


def test_unfinished_jobs_are_not_read(monkeypatch):
    _both(monkeypatch)
    _reports(monkeypatch)
    fetched = _gitlab(monkeypatch, "running",
                      [_job(1, "sonar", "running"), _job(2, "bd", "manual"),
                       _job(3, "detect", "skipped"), _job(4, "scan", "success")],
                      {})
    gitlab.gitlab_pipeline({"project": "g/p"})
    assert fetched == [4]


def test_a_polling_snapshot_never_searches(monkeypatch):
    _both(monkeypatch)
    _reports(monkeypatch)
    fetched = _gitlab(monkeypatch, "running", [_job(3, "sonar")], {3: SONAR_LOG})
    out = gitlab.gitlab_pipeline({"project": "g/p"}, skip_jobs=True)
    assert fetched == [] and "scans" not in out
