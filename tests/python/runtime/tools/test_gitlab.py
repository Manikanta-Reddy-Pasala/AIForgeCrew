"""GitLab tool — REST v4 shapes mocked at the urllib layer."""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime.tools import gitlab as gl
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
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.internal")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-123")
    monkeypatch.delenv("GITLAB_PROJECT", raising=False)
    monkeypatch.delenv("GITLAB_OAUTH", raising=False)


def _capture(monkeypatch, *payloads):
    """Queue one payload per urlopen call (read uses the next, in order)."""
    seen = {"calls": []}
    q = list(payloads)

    def fake_urlopen(req, timeout=None, context=None):
        call = {
            "method": req.get_method(),
            "url": req.full_url,
            "headers": dict(req.header_items()),
            "body": json.loads(req.data.decode()) if req.data else None,
            "context": context,
        }
        seen["calls"].append(call)
        seen.update(call)            # last-call convenience accessors
        return _Resp(q.pop(0) if q else None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_not_configured(monkeypatch):
    monkeypatch.delenv("GITLAB_BASE_URL", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    assert gl.gitlab_read({"project": "g/p", "iid": 1})["error"] == "gitlab_not_configured"


def test_search_global(cfg, monkeypatch):
    seen = _capture(monkeypatch, [
        {"iid": 7, "project_id": 3, "title": "Bug", "state": "opened",
         "labels": ["x"], "assignee": {"name": "Alice"},
         "web_url": "https://gitlab.internal/g/p/-/issues/7"}])
    out = gl.gitlab_search({"query": "deploy"})
    assert out["ok"]
    assert out["results"][0]["iid"] == 7
    assert out["results"][0]["assignee"] == "Alice"
    assert "/api/v4/issues" in seen["url"]
    assert "scope=all" in seen["url"]
    assert seen["headers"].get("Private-token") == "glpat-123"


def test_search_project_scoped(cfg, monkeypatch):
    seen = _capture(monkeypatch, [])
    gl.gitlab_search({"query": "x", "project": "group/proj", "state": "opened"})
    assert "/projects/group%2Fproj/issues" in seen["url"]
    assert "state=opened" in seen["url"]


def test_read(cfg, monkeypatch):
    seen = _capture(
        monkeypatch,
        {"iid": 42, "title": "T", "description": "desc", "state": "opened",
         "author": {"name": "Ann"}, "assignee": {"name": "Bob"},
         "labels": ["a", "b"], "web_url": "https://gitlab.internal/g/p/-/issues/42"},
        [{"author": {"name": "Cara"}, "body": "hi"},
         {"author": {"name": "sys"}, "body": "changed", "system": True}])
    out = gl.gitlab_read({"project": "g/p", "iid": 42})
    assert out["ok"]
    assert out["description"] == "desc"
    assert out["state"] == "opened"
    assert out["labels"] == ["a", "b"]
    assert out["comments"] == [{"author": "Cara", "body": "hi"}]   # system note filtered
    assert out["url"].endswith("/issues/42")


def test_read_requires_project(cfg):
    assert gl.gitlab_read({"iid": 1})["error"] == "missing 'project'"


def test_read_uses_default_project(monkeypatch):
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.internal")
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_PROJECT", "team/repo")
    seen = _capture(monkeypatch, {"iid": 5}, [])
    out = gl.gitlab_read({"iid": 5})
    assert out["ok"]
    assert "/projects/team%2Frepo/issues/5" in seen["calls"][0]["url"]


def test_create(cfg, monkeypatch):
    seen = _capture(monkeypatch, {"iid": 99,
                                  "web_url": "https://gitlab.internal/g/p/-/issues/99"})
    out = gl.gitlab_create({"project": "g/p", "title": "New", "description": "d",
                            "labels": ["x", "y"]})
    assert out["ok"]
    assert out["iid"] == 99
    assert seen["method"] == "POST"
    assert seen["body"]["title"] == "New"
    assert seen["body"]["labels"] == "x,y"
    assert out["url"].endswith("/issues/99")


def test_create_requires_title(cfg):
    assert gl.gitlab_create({"project": "g/p"})["error"] == "missing 'title'"


def test_update(cfg, monkeypatch):
    seen = _capture(monkeypatch, {"iid": 10})
    out = gl.gitlab_update({"project": "g/p", "iid": 10, "title": "Edited",
                            "labels": ["z"], "state_event": "close"})
    assert out["ok"]
    assert out["iid"] == 10
    assert seen["method"] == "PUT"
    assert seen["body"]["title"] == "Edited"
    assert seen["body"]["labels"] == "z"
    assert seen["body"]["state_event"] == "close"


def test_update_requires_fields(cfg):
    assert gl.gitlab_update({"project": "g/p", "iid": 1})["error"] == "no fields to update"


def test_comment(cfg, monkeypatch):
    seen = _capture(monkeypatch, {"id": 555})
    out = gl.gitlab_comment({"project": "g/p", "iid": 10, "body": "looks good"})
    assert out["ok"]
    assert out["id"] == 555
    assert seen["method"] == "POST"
    assert "/projects/g%2Fp/issues/10/notes" in seen["url"]
    assert seen["body"]["body"] == "looks good"


def test_oauth_uses_bearer(monkeypatch):
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.internal")
    monkeypatch.setenv("GITLAB_TOKEN", "oauth-tok")
    monkeypatch.setenv("GITLAB_OAUTH", "1")
    seen = _capture(monkeypatch, [])
    gl.gitlab_search({"query": "x"})
    assert seen["headers"].get("Authorization") == "Bearer oauth-tok"
    assert "Private-token" not in seen["headers"]


def test_insecure_tls_builds_a_pinned_context(monkeypatch):
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.internal")
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_INSECURE_TLS", "1")
    import ssl
    seen = _capture(monkeypatch, [])
    gl.gitlab_search({"query": "x"})
    ctx = seen["context"]
    assert ctx is not None
    # "insecure_tls" now means PINNED, not unverified — see net/trust.py.
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_reads_stored_config_when_env_absent(tmp_path, monkeypatch):
    for k in ("GITLAB_BASE_URL", "GITLAB_TOKEN", "GITLAB_PROJECT",
              "GITLAB_OAUTH", "GITLAB_INSECURE_TLS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import integrations
    integrations.set_("gitlab",
                      {"base_url": "https://s.internal", "token": "stored-tok"})
    seen = _capture(monkeypatch, [])
    assert gl.gitlab_search({"query": "x"})["ok"]
    assert seen["headers"]["Private-token"] == "stored-tok"
    assert "s.internal" in seen["url"]


def test_env_wins_over_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import integrations
    integrations.set_("gitlab", {"base_url": "https://stored", "token": "stored"})
    monkeypatch.setenv("GITLAB_BASE_URL", "https://env.internal")
    monkeypatch.setenv("GITLAB_TOKEN", "env-tok")
    seen = _capture(monkeypatch, [])
    gl.gitlab_search({"query": "x"})
    assert "env.internal" in seen["url"]
    assert seen["headers"]["Private-token"] == "env-tok"


def test_writes_default_to_ask_policy(monkeypatch):
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_TOOL_POLICY", raising=False)
    assert tool_policy.decide("gitlab_update", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("gitlab_create", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("gitlab_comment", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("gitlab_read", {})["policy"] == tool_policy.ALLOW
    monkeypatch.setenv("AIFORGE_TOOL_POLICY", "gitlab_update=allow")
    assert tool_policy.decide("gitlab_update", {})["policy"] == tool_policy.ALLOW
