"""Black Duck tool: the token exchange, addressing a project version, and
reading its policy status and vulnerabilities.

Sections are faked at ``_request``; the exchange is faked one layer down, at
the shared ``http_request``, because the exchange IS the wire behaviour.
"""
import pytest

from aiforge_core.runtime.tools import _http_integration as H
from aiforge_core.runtime.tools import blackduck

BASE = "https://bd.test"
PID = "11111111-2222-3333-4444-555555555555"
VID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
VPATH = f"/api/projects/{PID}/versions/{VID}"
LINK = BASE + VPATH + "/components"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("BLACKDUCK_BASE_URL", BASE)
    monkeypatch.setenv("BLACKDUCK_TOKEN", "bd-t0ken")
    from aiforge_core.config import _filecache
    _filecache.clear()
    blackduck._bearers.clear()
    yield
    blackduck._bearers.clear()


def _comp(name, status="NOT_IN_VIOLATION", cid="c0000000-0000-0000-0000-000000000001"):
    return {"componentName": name, "componentVersionName": "1.0",
            "policyStatus": status,
            "licenses": [{"licenseDisplay": "GPL-3.0"}],
            "_meta": {"href": f"https://elsewhere.test{VPATH}/components/{cid}"
                              f"/versions/{cid}"}}


def _vuln(name, severity, score, status="NEW", comp="log4j"):
    return {"componentName": comp, "componentVersionName": "2.14",
            "vulnerabilityWithRemediation": {
                "vulnerabilityName": name, "severity": severity,
                "overallScore": score, "remediationStatus": status}}


ROUTES = {
    VPATH: {"versionName": "main"},
    f"/api/projects/{PID}": {"name": "shop"},
    VPATH + "/policy-status": {
        "overallStatus": "IN_VIOLATION",
        "componentVersionStatusCounts": [{"name": "IN_VIOLATION", "value": 1},
                                         {"name": "NOT_IN_VIOLATION", "value": 2},
                                         {"name": "IN_VIOLATION_OVERRIDDEN", "value": 0}]},
    VPATH + "/components": {"totalCount": 3, "items": [
        _comp("requests"), _comp("gpl-lib", "IN_VIOLATION"), _comp("flask")]},
    VPATH + "/vulnerable-bom-components": {"totalCount": 4, "items": [
        _vuln("CVE-2020-1", "MEDIUM", 5.0),
        _vuln("CVE-2021-44228", "CRITICAL", 10.0),
        _vuln("CVE-2019-9", "HIGH", 7.5, status="NOT_AFFECTED"),
        _vuln("CVE-2022-2", "HIGH", 8.1)]},
}


def _fake(monkeypatch, routes, calls=None):
    def _req(path, params=None):
        if calls is not None:
            calls.append((path, dict(params or {})))
        val = routes.get(path)
        if val is None and path.endswith("/policy-rules"):
            val = {"items": [{"name": "No GPL", "severity": "BLOCKER"}]}
        if val is None:
            return {"ok": False, "error": "http 404"}
        if isinstance(val, dict) and "ok" in val:
            return val
        return {"ok": True, "data": val}
    monkeypatch.setattr(blackduck, "_request", _req)


# ── addressing ──────────────────────────────────────────────────────

@pytest.mark.parametrize("link, want", [
    (LINK, {"project_id": PID, "version_id": VID}),
    (f"{BASE}/ui/projects/{PID}/versions/{VID}/components?filter=x",
     {"project_id": PID, "version_id": VID}),
    (f"{BASE}/api/projects/{PID}", {"project_id": PID, "version_id": ""}),
    (BASE + "/api/projects/not-an-id/versions/also-not", {}),
    ("", {}),
])
def test_parse_link(link, want):
    assert blackduck.parse_link(link) == want


def test_a_link_on_another_host_is_refused_without_a_request(monkeypatch):
    calls = []
    _fake(monkeypatch, ROUTES, calls)
    out = blackduck.blackduck_report({"url": "https://evil.test" + VPATH})
    assert not out["ok"]
    assert out["error"] == "link_not_on_configured_blackduck"
    assert calls == []


def test_by_project_and_version_name(monkeypatch):
    calls = []
    _fake(monkeypatch, {
        **ROUTES,
        "/api/projects": {"items": [
            {"name": "shop-admin", "_meta": {"href": BASE + "/api/projects/" + VID}},
            {"name": "Shop", "_meta": {"href": BASE + "/api/projects/" + PID}}]},
        f"/api/projects/{PID}/versions": {"items": [
            {"versionName": "main", "_meta": {"href": BASE + VPATH}}]},
    }, calls)
    out = blackduck.blackduck_report({"project": "shop", "version": "main"})
    assert out["ok"] and out["project"] == "shop" and out["version"] == "main"
    assert calls[0] == ("/api/projects", {"q": "name:shop", "limit": 100})
    assert calls[1] == (f"/api/projects/{PID}/versions",
                        {"limit": 25, "q": "versionName:main"})


def test_an_unknown_project_names_the_near_misses(monkeypatch):
    _fake(monkeypatch, {"/api/projects": {"items": [{"name": "shop-admin"}]}})
    out = blackduck.blackduck_report({"project": "shop"})
    assert out["error"] == "project_not_found"
    assert out["similar"] == ["shop-admin"]


def test_several_versions_and_none_named_asks_which(monkeypatch):
    _fake(monkeypatch, {
        "/api/projects": {"items": [
            {"name": "shop", "_meta": {"href": BASE + "/api/projects/" + PID}}]},
        f"/api/projects/{PID}/versions": {"items": [
            {"versionName": "main"}, {"versionName": "feat-x"}]}})
    out = blackduck.blackduck_report({"project": "shop"})
    assert out["error"] == "missing 'version'"
    assert out["versions"] == ["main", "feat-x"]


def test_neither_link_nor_project_is_an_error(monkeypatch):
    _fake(monkeypatch, ROUTES)
    assert "project" in blackduck.blackduck_report({})["error"]


def test_not_configured(monkeypatch):
    monkeypatch.delenv("BLACKDUCK_TOKEN")
    assert blackduck.blackduck_report({"url": LINK})["error"] == \
        "blackduck_not_configured"


# ── the report ──────────────────────────────────────────────────────

def test_report_from_the_detect_link(monkeypatch):
    calls = []
    _fake(monkeypatch, ROUTES, calls)
    out = blackduck.blackduck_report({"url": LINK})
    assert out["ok"] and out["project"] == "shop" and out["version"] == "main"
    assert out["url"] == LINK
    assert out["policy"] == {"status": "IN_VIOLATION", "in_violation": True,
                             "components": {"IN_VIOLATION": 1,
                                            "NOT_IN_VIOLATION": 2}}
    bad = out["policy_violations"]
    assert bad["count"] == 1
    assert bad["shown"] == [{"component": "gpl-lib", "version": "1.0",
                             "license": "GPL-3.0",
                             "rules": [{"rule": "No GPL", "severity": "BLOCKER"}]}]
    vulns = out["vulnerabilities"]
    assert vulns["open"] == 3 and vulns["already_handled"] == 1
    assert vulns["by_severity"] == {"CRITICAL": 1, "HIGH": 1, "MEDIUM": 1}
    assert [v["id"] for v in vulns["shown"]] == [
        "CVE-2021-44228", "CVE-2022-2", "CVE-2020-1"]


def test_a_component_link_is_followed_only_as_a_path_on_our_server(monkeypatch):
    """The component's own link names another host here. Only its path is
    used, under the configured server — the bearer never goes elsewhere."""
    calls = []
    _fake(monkeypatch, ROUTES, calls)
    blackduck.blackduck_report({"url": LINK})
    rules = [path for path, _ in calls if path.endswith("/policy-rules")]
    assert len(rules) == 1
    assert rules[0].startswith(VPATH + "/components/")
    assert "elsewhere" not in rules[0]


def test_a_clean_policy_skips_the_component_walk(monkeypatch):
    calls = []
    _fake(monkeypatch, {**ROUTES, VPATH + "/policy-status":
                        {"overallStatus": "NOT_IN_VIOLATION"}}, calls)
    out = blackduck.blackduck_report({"url": LINK})
    assert out["policy"]["in_violation"] is False
    assert "policy_violations" not in out
    assert VPATH + "/components" not in [path for path, _ in calls]


def test_limit_caps_the_rows_and_says_so(monkeypatch):
    _fake(monkeypatch, ROUTES)
    vulns = blackduck.blackduck_report({"url": LINK, "limit": 1})["vulnerabilities"]
    assert [v["id"] for v in vulns["shown"]] == ["CVE-2021-44228"]
    assert vulns["open"] == 3 and "1 most severe of 3" in vulns["note"]


def test_a_list_longer_than_we_read_is_reported(monkeypatch):
    monkeypatch.setattr(blackduck, "_MAX_PAGES", 2)
    monkeypatch.setattr(blackduck, "_PAGE", 2)
    page = {"totalCount": 9, "items": [_vuln("CVE-1", "LOW", 1.0),
                                       _vuln("CVE-2", "LOW", 1.0)]}
    _fake(monkeypatch, {**ROUTES, VPATH + "/vulnerable-bom-components": page})
    vulns = blackduck.blackduck_report({"url": LINK})["vulnerabilities"]
    assert vulns["open"] == 4
    assert "only the first 4 rows" in vulns["note"]


def test_a_page_that_cannot_be_parsed_is_not_an_empty_list(monkeypatch):
    _fake(monkeypatch, {**ROUTES, VPATH + "/vulnerable-bom-components":
                        {"ok": True, "data": "<html>", "body_cap_hit": 2_000_000}})
    vulns = blackduck.blackduck_report({"url": LINK})["vulnerabilities"]
    assert vulns["open"] == 0 and "could not be parsed" in vulns["note"]


def test_a_nonsense_limit_falls_back_to_the_default(monkeypatch):
    _fake(monkeypatch, ROUTES)
    out = blackduck.blackduck_report({"url": LINK, "limit": float("inf")})
    assert out["ok"] and len(out["vulnerabilities"]["shown"]) == 3


def test_a_missing_version_is_the_answer(monkeypatch):
    _fake(monkeypatch, {})
    out = blackduck.blackduck_report({"url": LINK})
    assert not out["ok"] and out["error"] == "http 404"


# ── the token exchange ──────────────────────────────────────────────

def _wire(monkeypatch, answers):
    """Fake http_request. ``answers`` maps a URL suffix to a list of replies,
    popped in order (the last one repeats)."""
    calls = []

    def _http(method, url, **kw):
        calls.append({"method": method, "url": url, **kw})
        for suffix, replies in answers.items():
            if url.split("?")[0].endswith(suffix):
                return replies.pop(0) if len(replies) > 1 else replies[0]
        return {"ok": False, "error": "http 404"}
    monkeypatch.setattr(H, "http_request", _http)
    return calls


def _bearer_reply(token="B1", ms=7_200_000):
    return {"ok": True, "data": {"bearerToken": token,
                                 "expiresInMilliseconds": ms}}


def test_the_api_token_is_exchanged_once_and_the_bearer_reused(monkeypatch):
    calls = _wire(monkeypatch, {
        "/api/tokens/authenticate": [_bearer_reply()],
        "/api/current-user": [{"ok": True, "data": {"userName": "ci"}}]})
    assert blackduck._request("/api/current-user")["ok"]
    assert blackduck._request("/api/current-user")["ok"]
    exchanges = [c for c in calls if c["url"].endswith("/authenticate")]
    assert len(exchanges) == 1
    ex = exchanges[0]
    assert ex["method"] == "POST" and ex["url"] == BASE + "/api/tokens/authenticate"
    assert ex["headers"]["Authorization"] == "token bd-t0ken"
    assert ex["credential_exchange"] is True and "body" not in ex
    reads = [c for c in calls if c["url"].endswith("/current-user")]
    assert all(c["headers"]["Authorization"] == "Bearer B1" for c in reads)
    assert all(c["method"] == "GET" for c in reads)


def test_a_bearer_the_server_dropped_is_exchanged_again_once(monkeypatch):
    calls = _wire(monkeypatch, {
        "/api/tokens/authenticate": [_bearer_reply("B1"), _bearer_reply("B2")],
        "/api/current-user": [{"ok": False, "error": "http 401"},
                              {"ok": True, "data": {"userName": "ci"}}]})
    assert blackduck._request("/api/current-user")["ok"]
    used = [c["headers"]["Authorization"] for c in calls
            if c["url"].endswith("/current-user")]
    assert used == ["Bearer B1", "Bearer B2"]


def test_a_token_that_stays_rejected_is_reported_not_looped(monkeypatch):
    calls = _wire(monkeypatch, {
        "/api/tokens/authenticate": [_bearer_reply()],
        "/api/current-user": [{"ok": False, "error": "http 401"}]})
    out = blackduck._request("/api/current-user")
    assert not out["ok"] and "rejected" in out["hint"]
    assert len([c for c in calls if c["url"].endswith("/current-user")]) == 2


def test_a_failed_exchange_is_the_answer(monkeypatch):
    _wire(monkeypatch, {"/api/tokens/authenticate":
                        [{"ok": False, "error": "http 401"}]})
    out = blackduck.blackduck_test()
    assert not out["ok"] and out["error"] == "http 401"
    assert out["base_url"] == BASE and "rejected" in out["hint"]


def test_an_expired_bearer_is_not_reused(monkeypatch):
    calls = _wire(monkeypatch, {
        "/api/tokens/authenticate": [_bearer_reply("B1", ms=30_000),
                                     _bearer_reply("B2")],
        "/api/current-user": [{"ok": True, "data": {}}]})
    blackduck._request("/api/current-user")
    blackduck._request("/api/current-user")
    # 30 s of life is inside the one-minute safety margin: never held.
    assert len([c for c in calls if c["url"].endswith("/authenticate")]) == 2


def test_connection_test(monkeypatch):
    _wire(monkeypatch, {
        "/api/tokens/authenticate": [_bearer_reply()],
        "/api/current-user": [{"ok": True, "data": {"userName": "ci-reader"}}]})
    assert blackduck.blackduck_test() == {"ok": True, "base_url": BASE,
                                          "user": "ci-reader"}


def test_an_unreadable_ca_bundle_is_an_answer_not_a_raise(monkeypatch):
    monkeypatch.setenv("BLACKDUCK_CA_BUNDLE", "/no/such/ca.pem")
    out = blackduck.blackduck_report({"url": LINK})
    assert not out["ok"] and "ca.pem" in out["error"]
    assert not blackduck.blackduck_test()["ok"]


# ── the shared helper's exchange flag ───────────────────────────────

def test_a_credential_exchange_is_judged_as_a_read_and_carries_no_body(monkeypatch):
    from aiforge_core.net import egress
    seen = []
    monkeypatch.setattr(egress, "allow",
                        lambda kind, url, method="GET", **kw:
                        seen.append(method) or {"ok": False, "error": "stop"})
    H.http_request("POST", BASE + "/x", headers={}, credential_exchange=True)
    H.http_request("POST", BASE + "/x", headers={})
    assert seen == ["GET", "POST"]
    out = H.http_request("POST", BASE + "/x", headers={}, body={"a": 1},
                         credential_exchange=True)
    assert not out["ok"] and "no body" in out["error"]
    assert seen == ["GET", "POST"]      # refused before the policy was asked
