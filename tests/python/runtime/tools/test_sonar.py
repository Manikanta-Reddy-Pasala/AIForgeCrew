"""SonarQube tool: addressing a scan by link or project key, and reading it.

The Sonar API is faked at ``_request`` — the layer under every section and
above the shared HTTP helper — so these exercise the addressing, the version
fallbacks and the report shape without a network.
"""
import base64

import pytest

from aiforge_core.runtime.tools import sonar

BASE = "https://sonar.test"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("SONAR_BASE_URL", BASE)
    monkeypatch.setenv("SONAR_TOKEN", "squ_t0ken")
    from aiforge_core.config import _filecache
    _filecache.clear()
    yield


GATE_FAILED = {"projectStatus": {"status": "ERROR", "conditions": [
    {"status": "ERROR", "metricKey": "new_coverage", "comparator": "LT",
     "errorThreshold": "80", "actualValue": "41.2"},
    {"status": "OK", "metricKey": "new_bugs", "comparator": "GT",
     "errorThreshold": "0", "actualValue": "0"}]}}
ISSUES = {"paging": {"total": 37}, "issues": [
    {"severity": "BLOCKER", "type": "BUG", "rule": "python:S5754",
     "component": "shop:src/cart.py", "line": 12, "message": "Re-raise it."},
    {"severity": "MAJOR", "type": "CODE_SMELL", "rule": "python:S3776",
     "component": "shop:src/pay.py", "line": 80, "message": "Too complex."}],
    "facets": [{"property": "severities", "values": [
        {"val": "BLOCKER", "count": 1}, {"val": "MAJOR", "count": 36},
        {"val": "INFO", "count": 0}]},
        {"property": "types", "values": [{"val": "BUG", "count": 1},
                                         {"val": "CODE_SMELL", "count": 36}]}]}
HOTSPOTS = {"paging": {"total": 1}, "hotspots": [
    {"vulnerabilityProbability": "HIGH", "securityCategory": "sql-injection",
     "component": "shop:src/db.py", "line": 7, "message": "Check this query."}]}
MEASURES = {"component": {"measures": [
    {"metric": "coverage", "value": "63.0"},
    {"metric": "new_coverage", "period": {"value": "41.2"}},
    {"metric": "bugs", "periods": [{"value": "1"}]}]}}
ROUTES = {"/api/qualitygates/project_status": GATE_FAILED,
          "/api/issues/search": ISSUES, "/api/hotspots/search": HOTSPOTS,
          "/api/measures/component": MEASURES}


def _fake(monkeypatch, routes, calls=None):
    def _req(path, params=None, *, parse_json=True):
        if calls is not None:
            calls.append((path, dict(params or {})))
        val = routes.get(path)
        if callable(val):
            val = val(dict(params or {}))
        if val is None:
            return {"ok": False, "error": "http 404"}
        if isinstance(val, dict) and "ok" in val:
            return val
        return {"ok": True, "data": val}
    monkeypatch.setattr(sonar, "_request", _req)


# ── addressing ──────────────────────────────────────────────────────

@pytest.mark.parametrize("link, want", [
    (BASE + "/dashboard?id=shop", {"project": "shop", "branch": "", "pull_request": ""}),
    (BASE + "/dashboard?id=shop&pullRequest=12",
     {"project": "shop", "branch": "", "pull_request": "12"}),
    (BASE + "/project/issues?id=shop&branch=feat%2Fx&resolved=false",
     {"project": "shop", "branch": "feat/x", "pull_request": ""}),
    (BASE + "/api/ce/task?id=AYx-1", {"task_id": "AYx-1"}),
    (BASE + "/api/ce/task", {}),
    (BASE + "/about", {}),
    ("not a link", {}),
])
def test_parse_link(link, want):
    assert sonar.parse_link(link) == want


def test_a_link_on_another_host_is_refused_without_a_request(monkeypatch):
    calls = []
    _fake(monkeypatch, ROUTES, calls)
    out = sonar.sonar_report({"url": "https://evil.test/dashboard?id=shop"})
    assert not out["ok"]
    assert out["error"] == "link_not_on_configured_sonar"
    assert calls == []


def test_a_look_alike_host_is_not_our_server():
    assert sonar.on_our_server(BASE + "/dashboard?id=x")
    assert not sonar.on_our_server("https://sonar.test.evil.io/dashboard?id=x")
    assert not sonar.on_our_server("https://sonar.test:8443/dashboard?id=x")
    assert not sonar.on_our_server("")


def test_no_project_and_no_link_is_an_error(monkeypatch):
    _fake(monkeypatch, ROUTES)
    out = sonar.sonar_report({})
    assert not out["ok"] and "project" in out["error"]


def test_not_configured(monkeypatch):
    monkeypatch.delenv("SONAR_TOKEN")
    out = sonar.sonar_report({"project": "shop"})
    assert out == {"ok": False, "error": "sonar_not_configured",
                   "hint": out["hint"]}


# ── the report ──────────────────────────────────────────────────────

def test_report_from_a_dashboard_link(monkeypatch):
    calls = []
    _fake(monkeypatch, ROUTES, calls)
    out = sonar.sonar_report({"url": BASE + "/dashboard?id=shop&pullRequest=12"})
    assert out["ok"]
    assert out["project"] == "shop" and out["pull_request"] == "12"
    assert out["url"] == BASE + "/dashboard?id=shop&pullRequest=12"
    gate = out["quality_gate"]
    assert gate["status"] == "ERROR" and gate["passed"] is False
    assert gate["failed_conditions"] == [
        {"metric": "new_coverage", "actual": "41.2", "comparator": "LT",
         "threshold": "80"}]
    issues = out["issues"]
    assert issues["total"] == 37
    assert issues["by_severity"] == {"BLOCKER": 1, "MAJOR": 36}
    assert issues["shown"][0] == {
        "severity": "BLOCKER", "type": "BUG", "rule": "python:S5754",
        "file": "src/cart.py", "line": 12, "message": "Re-raise it."}
    assert "2 most severe of 37" in issues["note"]
    assert out["hotspots"]["to_review"] == 1
    assert out["hotspots"]["shown"][0]["file"] == "src/db.py"
    assert out["measures"] == {"coverage": "63.0", "new_coverage": "41.2",
                               "bugs": "1"}
    # Every project call carries the pull request, or it reads the main branch.
    for path, params in calls:
        assert params.get("pullRequest") == "12", path


def test_issue_search_is_scoped_to_the_project(monkeypatch):
    calls = []
    _fake(monkeypatch, ROUTES, calls)
    sonar.sonar_report({"project": "shop", "branch": "main", "limit": 5})
    params = dict(calls)["/api/issues/search"]
    assert params["componentKeys"] == "shop"
    assert params["resolved"] == "false" and params["ps"] == 5
    assert params["branch"] == "main" and "pullRequest" not in params


def test_a_renamed_parameter_is_retried_under_its_new_name(monkeypatch):
    calls = []

    def issues(params):
        if "componentKeys" in params:
            return {"ok": False, "error": "http 400"}
        return ISSUES

    _fake(monkeypatch, {**ROUTES, "/api/issues/search": issues}, calls)
    out = sonar.sonar_report({"project": "shop"})
    assert out["issues"]["total"] == 37
    asked = [p for path, p in calls if path == "/api/issues/search"]
    assert [("componentKeys" in p, "components" in p) for p in asked] == [
        (True, False), (False, True)]
    assert asked[1]["components"] == "shop"


def test_a_section_that_fails_does_not_cost_the_rest(monkeypatch):
    _fake(monkeypatch, {**ROUTES,
                        "/api/hotspots/search": {"ok": False, "error": "http 500"}})
    out = sonar.sonar_report({"project": "shop"})
    assert out["ok"] and out["hotspots"] == {"error": "http 500"}
    assert out["issues"]["total"] == 37


def test_a_gate_that_cannot_be_read_is_the_answer(monkeypatch):
    _fake(monkeypatch, {**ROUTES, "/api/qualitygates/project_status":
                        {"ok": False, "error": "http 403"}})
    out = sonar.sonar_report({"project": "shop"})
    assert not out["ok"] and out["error"] == "http 403"
    assert "Browse" in out["hint"]


def test_a_passed_gate(monkeypatch):
    _fake(monkeypatch, {**ROUTES, "/api/qualitygates/project_status":
                        {"projectStatus": {"status": "OK", "conditions": []}}})
    gate = sonar.sonar_report({"project": "shop"})["quality_gate"]
    assert gate == {"status": "OK", "passed": True, "failed_conditions": []}


# ── the scanner's task link ─────────────────────────────────────────

def _task(status="SUCCESS", **kw):
    return {"task": {"componentKey": "shop", "status": status,
                     "analysisId": "AN-9", "pullRequest": "12", **kw}}


def test_a_task_link_names_the_project_and_the_analysis(monkeypatch):
    calls = []
    _fake(monkeypatch, {**ROUTES, "/api/ce/task": _task()}, calls)
    out = sonar.sonar_report({"url": BASE + "/api/ce/task?id=AYx-1"})
    assert out["ok"] and out["project"] == "shop" and out["pull_request"] == "12"
    assert calls[0] == ("/api/ce/task", {"id": "AYx-1"})
    # The gate of THIS analysis, not whatever the project's latest is.
    assert dict(calls)["/api/qualitygates/project_status"] == {"analysisId": "AN-9"}


@pytest.mark.parametrize("status, word", [("PENDING", "still processing"),
                                           ("IN_PROGRESS", "still processing"),
                                           ("FAILED", "could not process")])
def test_an_unfinished_analysis_is_not_read_as_a_result(monkeypatch, status, word):
    calls = []
    _fake(monkeypatch, {**ROUTES, "/api/ce/task":
                        _task(status, errorMessage="boom")}, calls)
    out = sonar.sonar_report({"url": BASE + "/api/ce/task?id=AYx-1"})
    assert out["ok"] and "quality_gate" not in out
    assert out["analysis"] == {"status": status, "error": "boom"}
    assert word in out["hint"]
    assert [path for path, _ in calls] == ["/api/ce/task"]


def test_a_task_link_the_token_may_not_read_says_what_to_pass(monkeypatch):
    _fake(monkeypatch, {**ROUTES, "/api/ce/task":
                        {"ok": False, "error": "http 403"}})
    out = sonar.sonar_report({"url": BASE + "/api/ce/task?id=AYx-1"})
    assert not out["ok"] and "Execute Analysis" in out["hint"]


def test_an_unreadable_ca_bundle_is_an_answer_not_a_raise(monkeypatch):
    monkeypatch.setenv("SONAR_CA_BUNDLE", "/no/such/ca.pem")
    out = sonar.sonar_report({"project": "shop"})
    assert not out["ok"] and "ca.pem" in out["error"]


# ── the wire ────────────────────────────────────────────────────────

def test_the_token_goes_as_basic_auth_with_an_empty_password():
    auth = sonar._headers()["Authorization"]
    assert auth.startswith("Basic ")
    assert base64.b64decode(auth[6:]).decode() == "squ_t0ken:"


def test_request_builds_the_url_and_drops_empty_parameters(monkeypatch):
    from aiforge_core.runtime.tools import _http_integration as H
    seen = {}

    def _http(method, url, **kw):
        seen.update(method=method, url=url)
        return {"ok": True, "data": {}}
    monkeypatch.setattr(H, "http_request", _http)
    sonar._request("/api/issues/search", {"componentKeys": "a b", "branch": ""})
    assert seen == {"method": "GET",
                    "url": BASE + "/api/issues/search?componentKeys=a+b"}


def test_connection_test(monkeypatch):
    _fake(monkeypatch, {"/api/authentication/validate": {"valid": True},
                        "/api/server/version": "10.6.0.92116\n"})
    assert sonar.sonar_test() == {"ok": True, "base_url": BASE,
                                  "version": "10.6.0.92116"}
    _fake(monkeypatch, {"/api/authentication/validate": {"valid": False}})
    out = sonar.sonar_test()
    assert not out["ok"] and out["error"] == "token rejected"
