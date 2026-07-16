"""Jira tool — REST shapes mocked at the urllib layer."""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime.tools import jira as jr
from aiforge_core.runtime.tools import tool_policy


class _Resp:
    def __init__(self, payload):
        self._b = b"" if payload is None else json.dumps(payload).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.internal")
    monkeypatch.setenv("JIRA_TOKEN", "pat-123")
    monkeypatch.delenv("JIRA_USER", raising=False)


def _capture(monkeypatch, payload):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["body"] = json.loads(req.data.decode()) if req.data else None
        seen["context"] = context
        return _Resp(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_not_configured(monkeypatch):
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    assert jr.jira_read({"key": "ENG-1"})["error"] == "jira_not_configured"


def test_search(cfg, monkeypatch):
    seen = _capture(monkeypatch, {"total": 1, "issues": [
        {"key": "ENG-10", "fields": {"summary": "Runbook",
         "issuetype": {"name": "Task"}, "status": {"name": "Open"},
         "assignee": {"displayName": "Alice"}}}]})
    out = jr.jira_search({"query": "deploy"})
    assert out["ok"] and out["results"][0]["key"] == "ENG-10"
    assert out["results"][0]["status"] == "Open"
    assert "/rest/api/2/search" in seen["url"]
    assert "text" in seen["url"]                       # jql built from query
    assert seen["headers"].get("Authorization") == "Bearer pat-123"


def _paged(monkeypatch, total):
    """Mock /search that honours startAt+maxResults over a set of `total` bugs."""
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        import urllib.parse as up
        calls.append(req.full_url)
        q = up.parse_qs(up.urlsplit(req.full_url).query)
        start = int(q["startAt"][0])
        mx = int(q["maxResults"][0])
        issues = [{"key": f"BT-{i}", "fields": {"summary": "s",
                   "issuetype": {"name": "Bug"}, "status": {"name": "Open"}}}
                  for i in range(start, min(start + mx, total))]
        return _Resp({"total": total, "startAt": start, "issues": issues})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def test_search_all_loops_pages_until_exhausted(cfg, monkeypatch):
    """99 bugs across pages → limit=all returns EVERY one, not just page 1."""
    monkeypatch.setattr(jr, "_SEARCH_PAGE", 40)          # force multi-page
    calls = _paged(monkeypatch, 99)
    out = jr.jira_search({"jql": "sprint = 'BT July-2026' AND type = Bug",
                          "limit": "all"})
    assert out["ok"]
    assert out["count"] == 99 and out["total"] == 99
    assert out["truncated"] is False
    assert len(out["results"]) == 99
    assert len(calls) == 3                               # 40 + 40 + 19


def test_search_default_limit_reports_truncation(cfg, monkeypatch):
    """No explicit limit → 50 returned, but truncated flag + total tell the
    agent 99 exist so it can re-ask with limit=all."""
    _paged(monkeypatch, 99)
    out = jr.jira_search({"jql": "type = Bug"})
    assert out["count"] == 50 and out["total"] == 99
    assert out["truncated"] is True


def test_search_explicit_limit_honoured(cfg, monkeypatch):
    _paged(monkeypatch, 99)
    out = jr.jira_search({"jql": "type = Bug", "limit": 99})
    assert out["count"] == 99 and out["truncated"] is False


def test_read_by_key(cfg, monkeypatch):
    _capture(monkeypatch, {"key": "ENG-10", "fields": {
        "summary": "Runbook", "description": "desc",
        "status": {"name": "Open"}, "issuetype": {"name": "Bug"},
        "labels": ["a", "b"],
        "comment": {"comments": [{"author": {"displayName": "Bob"},
                                  "body": "hi"}]}}})
    out = jr.jira_read({"key": "ENG-10"})
    assert out["ok"] and out["description"] == "desc" and out["status"] == "Open"
    assert out["labels"] == ["a", "b"]
    assert out["comments"][0]["author"] == "Bob"
    assert out["url"].endswith("/browse/ENG-10")


def test_create_requires_fields(cfg):
    assert jr.jira_create({"project": "ENG"})["error"] == "missing 'summary'"


def test_create(cfg, monkeypatch):
    seen = _capture(monkeypatch, {"key": "ENG-99"})
    out = jr.jira_create({"project": "ENG", "summary": "New",
                          "issuetype": "Bug", "description": "d",
                          "labels": "x, y", "parent": "ENG-1"})
    assert out["ok"] and out["key"] == "ENG-99"
    assert seen["method"] == "POST"
    assert seen["body"]["fields"]["project"]["key"] == "ENG"
    assert seen["body"]["fields"]["issuetype"]["name"] == "Bug"
    assert seen["body"]["fields"]["labels"] == ["x", "y"]
    assert seen["body"]["fields"]["parent"]["key"] == "ENG-1"
    assert out["url"].endswith("/browse/ENG-99")


def test_update_fields(cfg, monkeypatch):
    seen = _capture(monkeypatch, None)        # PUT → 204 No Content
    out = jr.jira_update({"key": "ENG-10", "summary": "Edited",
                          "labels": ["z"]})
    assert out["ok"] and out["key"] == "ENG-10"
    assert seen["method"] == "PUT"
    assert seen["body"]["fields"]["summary"] == "Edited"
    assert seen["body"]["fields"]["labels"] == ["z"]


def test_update_requires_fields(cfg):
    assert jr.jira_update({"key": "ENG-10"})["error"] == "no fields to update"


def test_comment(cfg, monkeypatch):
    seen = _capture(monkeypatch, {"id": "555"})
    out = jr.jira_comment({"key": "ENG-10", "body": "looks good"})
    assert out["ok"] and out["id"] == "555"
    assert seen["method"] == "POST"
    assert "/rest/api/2/issue/ENG-10/comment" in seen["url"]
    assert seen["body"]["body"] == "looks good"


def test_basic_auth_when_user_set(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.internal")
    monkeypatch.setenv("JIRA_TOKEN", "pw")
    monkeypatch.setenv("JIRA_USER", "alice")
    seen = _capture(monkeypatch, {"issues": []})
    jr.jira_search({"query": "x"})
    assert seen["headers"].get("Authorization", "").startswith("Basic ")


def test_insecure_tls_builds_unverified_context(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.internal")
    monkeypatch.setenv("JIRA_TOKEN", "tok")
    monkeypatch.setenv("JIRA_INSECURE_TLS", "1")
    import ssl
    seen = _capture(monkeypatch, {"issues": []})
    jr.jira_search({"query": "x"})
    ctx = seen["context"]
    assert ctx is not None and ctx.verify_mode == ssl.CERT_NONE


def test_reads_stored_config_when_env_absent(tmp_path, monkeypatch):
    for k in ("JIRA_BASE_URL", "JIRA_TOKEN", "JIRA_USER", "JIRA_INSECURE_TLS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import integrations
    integrations.set_("jira",
                      {"base_url": "https://s.internal", "token": "stored-tok"})
    seen = _capture(monkeypatch, {"issues": []})
    assert jr.jira_search({"query": "x"})["ok"]
    assert seen["headers"]["Authorization"] == "Bearer stored-tok"
    assert "s.internal" in seen["url"]


def test_env_wins_over_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import integrations
    integrations.set_("jira", {"base_url": "https://stored", "token": "stored"})
    monkeypatch.setenv("JIRA_BASE_URL", "https://env.internal")
    monkeypatch.setenv("JIRA_TOKEN", "env-tok")
    seen = _capture(monkeypatch, {"issues": []})
    jr.jira_search({"query": "x"})
    assert "env.internal" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer env-tok"


def test_writes_default_to_ask_policy(monkeypatch):
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_TOOL_POLICY", raising=False)
    assert tool_policy.decide("jira_update", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("jira_create", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("jira_comment", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("jira_read", {})["policy"] == tool_policy.ALLOW
    # explicit override wins
    monkeypatch.setenv("AIFORGE_TOOL_POLICY", "jira_update=allow")
    assert tool_policy.decide("jira_update", {})["policy"] == tool_policy.ALLOW


def test_read_fetches_and_analyses_image_and_doc_attachments(cfg, monkeypatch):
    _capture(monkeypatch, {"key": "ENG-10", "fields": {
        "summary": "S",
        "attachment": [
            {"filename": "diagram.png", "mimeType": "image/png",
             "content": "https://jira.internal/secure/attachment/1/diagram.png"},
            {"filename": "spec.pdf", "mimeType": "application/pdf",
             "content": "https://jira.internal/secure/attachment/2/spec.pdf"},
            {"filename": "video.mp4", "mimeType": "video/mp4",
             "content": "https://jira.internal/secure/attachment/3/video.mp4"},
        ]}})
    monkeypatch.setattr(jr._http, "http_get_bytes",
                        lambda *a, **k: {"ok": True, "bytes": b"DATA"})
    from aiforge_core.runtime import chat_media
    monkeypatch.setattr(chat_media, "analyze_attachment",
                        lambda name, raw, role="doer", mime="": {"filename": name,
                                                                 "description": f"desc:{name}"})
    out = jr.jira_read({"key": "ENG-10"})
    assert out["ok"] and "attachments" in out
    # Image + pdf analysed, the unsupported video skipped.
    assert [i["filename"] for i in out["attachments"]] == ["diagram.png", "spec.pdf"]
    assert out["attachments"][1]["description"] == "desc:spec.pdf"


def test_read_attachments_can_be_disabled(cfg, monkeypatch):
    _capture(monkeypatch, {"key": "ENG-10", "fields": {
        "summary": "S",
        "attachment": [{"filename": "d.png", "mimeType": "image/png",
                        "content": "https://jira.internal/x"}]}})
    out = jr.jira_read({"key": "ENG-10", "attachments": False})
    assert "attachments" not in out
