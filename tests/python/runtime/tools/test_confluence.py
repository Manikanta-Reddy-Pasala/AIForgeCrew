"""Confluence tool — REST shapes mocked at the urllib layer."""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime.tools import confluence as cf
from aiforge_core.runtime.tools import tool_policy


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://conf.internal")
    monkeypatch.setenv("CONFLUENCE_TOKEN", "pat-123")
    monkeypatch.delenv("CONFLUENCE_USER", raising=False)


def _capture(monkeypatch, payload):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["body"] = json.loads(req.data.decode()) if req.data else None
        return _Resp(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_not_configured(monkeypatch):
    monkeypatch.delenv("CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.delenv("CONFLUENCE_TOKEN", raising=False)
    assert cf.confluence_read({"id": "1"})["error"] == "confluence_not_configured"


def test_search(cfg, monkeypatch):
    seen = _capture(monkeypatch, {"results": [
        {"id": "10", "title": "Runbook", "type": "page",
         "space": {"key": "ENG"}}]})
    out = cf.confluence_search({"query": "deploy"})
    assert out["ok"]
    assert out["results"][0]["id"] == "10"
    assert "/rest/api/content/search" in seen["url"]
    assert "text" in seen["url"]                       # cql built from query
    assert seen["headers"].get("Authorization") == "Bearer pat-123"


def test_read_by_id(cfg, monkeypatch):
    _capture(monkeypatch, {"id": "10", "title": "Runbook",
                           "space": {"key": "ENG"},
                           "version": {"number": 4},
                           "body": {"storage": {"value": "<p>hi</p>"}}})
    out = cf.confluence_read({"id": "10"})
    assert out["ok"]
    assert out["body"] == "<p>hi</p>"
    assert out["version"] == 4


def test_create_requires_fields(cfg):
    assert cf.confluence_create({"title": "x"})["error"] == "missing 'space'"


def test_create(cfg, monkeypatch):
    calls = _recording(monkeypatch, lambda req: {
        "id": "99", "title": "New", "status": "draft",
        "_links": {"webui": "/display/ENG/New"}})
    out = cf.confluence_create({"title": "New", "space": "ENG",
                                "body": "<p>x</p>", "parent_id": "5"})
    assert out["ok"]
    assert out["id"] == "99"
    post = calls[0]                                  # the create; a read-back GET follows
    assert post.get_method() == "POST"
    body = json.loads(post.data.decode())
    assert body["status"] == "current"               # published by default…
    assert body["title"] == "[DRAFT] New"            # …and marked as a draft
    assert "DRAFT" not in body["body"]["storage"]["value"]    # the title is the only mark
    assert body["space"]["key"] == "ENG"
    assert body["ancestors"] == [{"id": "5"}]
    assert out["url"].endswith("/display/ENG/New")


def test_update_increments_version(cfg, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None, context=None):
        calls["n"] += 1
        if req.get_method() == "GET":
            return _Resp({"id": "10", "title": "Old", "version": {"number": 7}})
        # PUT
        assert json.loads(req.data.decode())["version"]["number"] == 8
        return _Resp({"id": "10", "_links": {"webui": "/x"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = cf.confluence_update({"id": "10", "body": "<p>new</p>"})
    assert out["ok"]
    assert out["version"] == 8
    assert out["title"] == "Old"
    assert calls["n"] == 2


def test_basic_auth_when_user_set(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://conf.internal")
    monkeypatch.setenv("CONFLUENCE_TOKEN", "pw")
    monkeypatch.setenv("CONFLUENCE_USER", "alice")
    seen = _capture(monkeypatch, {"results": []})
    cf.confluence_search({"query": "x"})
    assert seen["headers"].get("Authorization", "").startswith("Basic ")


def test_reads_stored_config_when_env_absent(tmp_path, monkeypatch):
    for k in ("CONFLUENCE_BASE_URL", "CONFLUENCE_TOKEN", "CONFLUENCE_USER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import integrations
    integrations.set_("confluence",
                      {"base_url": "https://s.internal", "token": "stored-tok"})
    seen = _capture(monkeypatch, {"results": []})
    assert cf.confluence_search({"query": "x"})["ok"]
    assert seen["headers"]["Authorization"] == "Bearer stored-tok"
    assert "s.internal" in seen["url"]


def test_env_wins_over_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import integrations
    integrations.set_("confluence",
                      {"base_url": "https://stored", "token": "stored"})
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://env.internal")
    monkeypatch.setenv("CONFLUENCE_TOKEN", "env-tok")
    seen = _capture(monkeypatch, {"results": []})
    cf.confluence_search({"query": "x"})
    assert "env.internal" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer env-tok"


def test_writes_default_to_ask_policy(monkeypatch):
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_TOOL_POLICY", raising=False)
    assert tool_policy.decide("confluence_update", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("confluence_create", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("confluence_read", {})["policy"] == tool_policy.ALLOW
    # explicit override wins
    monkeypatch.setenv("AIFORGE_TOOL_POLICY", "confluence_update=allow")
    assert tool_policy.decide("confluence_update", {})["policy"] == tool_policy.ALLOW


def test_fetch_attachments_analyses_image_and_doc(cfg, monkeypatch):
    monkeypatch.setattr(cf, "_request", lambda *a, **k: {"ok": True, "data": {"results": [
        {"title": "chart.png", "extensions": {"mediaType": "image/png"},
         "_links": {"download": "/download/attachments/1/chart.png"}},
        {"title": "report.pdf", "extensions": {"mediaType": "application/pdf"},
         "_links": {"download": "/download/attachments/2/report.pdf"}},
        {"title": "clip.mp4", "extensions": {"mediaType": "video/mp4"},
         "_links": {"download": "/d"}}]}})
    monkeypatch.setattr(cf._http, "http_get_bytes",
                        lambda *a, **k: {"ok": True, "bytes": b"DATA"})
    from aiforge_core.runtime import chat_media
    monkeypatch.setattr(chat_media, "analyze_attachment",
                        lambda name, raw, role="doer", mime="": {"filename": name,
                                                                 "description": f"d:{name}"})
    out = cf._fetch_attachments("123")
    assert [i["filename"] for i in out] == ["chart.png", "report.pdf"]  # video skipped
    assert out[1]["description"] == "d:report.pdf"


def test_read_attaches_files(cfg, monkeypatch):
    _capture(monkeypatch, {"id": "10", "title": "Page",
                           "body": {"storage": {"value": "<p>hi</p>"}},
                           "space": {"key": "ENG"}, "version": {"number": 3}})
    monkeypatch.setattr(cf, "_fetch_attachments",
                        lambda pid, role="doer": [{"filename": "c.png",
                                                   "description": "a chart"}])
    out = cf.confluence_read({"id": "10"})
    assert out["ok"]
    assert out["attachments"][0]["description"] == "a chart"


def test_read_attachments_can_be_disabled(cfg, monkeypatch):
    _capture(monkeypatch, {"id": "10", "title": "P",
                           "body": {"storage": {"value": "x"}}})
    called = {"n": 0}
    monkeypatch.setattr(cf, "_fetch_attachments",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])
    out = cf.confluence_read({"id": "10", "attachments": False})
    assert "attachments" not in out
    assert called["n"] == 0


# ── media → storage macros (mermaid / code / image attachments) ──────────

def test_storagify_mermaid_code_and_image(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFLUENCE_DIAGRAM", "mermaid")   # test the macro path
    monkeypatch.delenv("AIFORGE_CONFLUENCE_MERMAID_MACRO", raising=False)
    body = ("```mermaid\ngraph TD;A-->B\n```\n\n"
            "```python\nprint(1)\n```\n\n"
            "![chart](/tmp/c.png)")
    out, refs = cf._storagify_media(body)
    assert '<ac:structured-macro ac:name="mermaid">' in out
    assert "graph TD;A-->B" in out
    assert '<ac:structured-macro ac:name="code">' in out
    assert '<ac:parameter ac:name="language">python</ac:parameter>' in out
    assert '<ac:image><ri:attachment ri:filename="c.png"/></ac:image>' in out
    assert refs == [{"filename": "c.png", "src": "/tmp/c.png"}]
    assert "```" not in out
    assert "![chart]" not in out


def test_parentheses_inside_flowchart_labels_are_quoted():
    src = ("flowchart TD\n"
           "  A[step (detail)] --> B{check (detail)}\n"
           "  C([Start]) --> D[(cylinder)]\n"
           "  E[\"already (quoted)\"]")
    out = cf.repair_mermaid(src)
    assert 'A["step (detail)"]' in out
    assert 'B{"check (detail)"}' in out
    assert "C([Start])" in out
    assert "D[(cylinder)]" in out
    # A label that is already quoted is not wrapped again.
    assert 'E["already (quoted)"]' in out
    edge = "flowchart TD\n  E -- see note[fig (a)] --> F\n"
    assert cf.repair_mermaid(edge) == edge
    quoted = ('flowchart TD\n  A["retry{n (max)}"]\n'
              '  B{"see A[step (detail)]"}\n')
    assert cf.repair_mermaid(quoted) == quoted
    slash = "flowchart TD\n  A[/Read config (yaml)/]\n"
    assert cf.repair_mermaid(slash) == slash
    assert cf.repair_mermaid("sequenceDiagram\n  A->>B: hi (x)").startswith(
        "sequenceDiagram")


def test_a_saved_page_gets_the_quoted_mermaid(monkeypatch):
    monkeypatch.delenv("AIFORGE_CONFLUENCE_DIAGRAM", raising=False)
    out, refs = cf._storagify_media(
        "```mermaid\nflowchart TD\n  A[step (detail)]\n```")
    assert 'A["step (detail)"]' in out
    assert refs == []


def test_storagify_mermaid_macro_env_override(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFLUENCE_DIAGRAM", "mermaid")
    monkeypatch.setenv("AIFORGE_CONFLUENCE_MERMAID_MACRO", "mermaid-cloud")
    out, _ = cf._storagify_media("```mermaid\nA-->B\n```")
    assert 'ac:name="mermaid-cloud"' in out


def test_storagify_passthrough_plain_storage():
    body = "<p>plain <strong>storage</strong> body, no fences</p>"
    out, refs = cf._storagify_media(body)
    assert out == body
    assert refs == []


def test_storagify_cdata_escapes_early_close():
    out, _ = cf._storagify_media("```\nvar x = ']]>';\n```")
    assert "]]]]><![CDATA[>" in out          # the ]]> was split, CDATA stays valid


def test_create_converts_and_uploads_image(cfg, monkeypatch, tmp_path):
    img = tmp_path / "diagram.png"
    img.write_bytes(b"\x89PNG\r\nfake")
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(req)
        if "/child/attachment" in req.full_url:
            return _Resp({"results": [{"id": "att1"}]})
        return _Resp({"id": "99", "_links": {"webui": "/display/ENG/New"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = cf.confluence_create({"title": "New", "space": "ENG",
                                "body": f"<p>see</p>\n\n![d]({img})\n"})
    assert out["ok"]
    create = [r for r in calls if "/child/attachment" not in r.full_url][0]
    sent = json.loads(create.data.decode())["body"]["storage"]["value"]
    assert '<ac:image><ri:attachment ri:filename="diagram.png"/></ac:image>' in sent
    assert "![" not in sent
    att = [r for r in calls if "/child/attachment" in r.full_url]
    assert att
    assert att[0].get_method() == "POST"
    assert att[0].headers.get("X-atlassian-token") == "nocheck"
    assert out["attachments"][0]["ok"]
    assert out["attachments"][0]["filename"] == "diagram.png"


# ── mermaid diagram modes (code default / mermaid macro) ─────────────────

def test_mermaid_default_mode_is_code_macro(monkeypatch):
    # DEFAULT: a code macro with the mermaid source, in place (renders anywhere)
    monkeypatch.delenv("AIFORGE_CONFLUENCE_DIAGRAM", raising=False)
    out, refs = cf._storagify_media("```mermaid\ngraph TD\n A-->B\n```")
    assert 'ac:name="code"' in out
    assert "drawio" not in out
    assert '<ac:parameter ac:name="language">mermaid</ac:parameter>' in out
    assert "graph TD" in out
    assert refs == []


def test_mermaid_mode_explicit_macro(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFLUENCE_DIAGRAM", "mermaid")
    out, _ = cf._storagify_media("```mermaid\ngraph TD\n A-->B\n```")
    assert 'ac:name="mermaid"' in out


# ── a page is "created" only if it can be read back; drafts say so ────────

def _recording(monkeypatch, handler):
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(req)
        return _Resp(handler(req))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def test_create_reads_the_page_back_and_reports_system_status_and_draft(cfg, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFLUENCE_PUBLISH_DEFAULT", "0")

    def handler(req):
        if req.get_method() == "POST":
            return {"id": "99", "title": "New", "status": "draft", "_links": {"webui": "/d/New"}}
        return {"id": "99", "title": "New", "status": "draft", "space": {"key": "ENG"}}
    calls = _recording(monkeypatch, handler)
    out = cf.confluence_create({"title": "New", "space": "ENG", "body": "<p>x</p>"})
    assert out["ok"] and out["verified"] and out["system"] == "confluence"
    assert out["status"] == "draft" and out["visible_in_space"] is False
    assert "DRAFT" in out["note"] and out["space"] == "ENG"
    assert [c.get_method() for c in calls][:2] == ["POST", "GET"]


def test_by_default_a_created_page_is_published_visible_and_marked(cfg, monkeypatch):
    def handler(req):
        return {"id": "99", "title": "[DRAFT] New", "status": "current",
                "space": {"key": "ENG"}, "_links": {"webui": "/d/New"}}
    calls = _recording(monkeypatch, handler)
    out = cf.confluence_create({"title": "New", "space": "ENG", "body": "<p>x</p>"})
    assert out["status"] == "current" and out["visible_in_space"] is True
    assert out["marked_as_draft"] is True and "[DRAFT]" in out["note"]
    assert not any("/label" in c.full_url for c in calls)   # no extras by default


def test_the_banner_and_label_are_opt_in(cfg, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFLUENCE_DRAFT_BANNER", "1")
    monkeypatch.setenv("AIFORGE_CONFLUENCE_DRAFT_LABEL", "1")
    calls = _recording(monkeypatch, lambda req: {
        "id": "99", "title": "[DRAFT] New", "status": "current", "space": {"key": "ENG"}})
    cf.confluence_create({"title": "New", "space": "ENG", "body": "<p>x</p>"})
    sent = json.loads(calls[0].data.decode())["body"]["storage"]["value"]
    assert "DRAFT" in sent and any("/label" in c.full_url for c in calls)


def test_a_page_that_cannot_be_read_back_is_not_reported_as_created(cfg, monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=None, context=None):
        if req.get_method() == "POST":
            return _Resp({"id": "99", "title": "New", "status": "draft"})
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = cf.confluence_create({"title": "New", "space": "ENG", "body": "<p>x</p>"})
    assert out["ok"] is False and "cannot be read back" in out["error"]


def test_a_response_without_an_id_is_not_a_created_page(cfg, monkeypatch):
    _capture(monkeypatch, {"title": "New"})
    out = cf.confluence_create({"title": "New", "space": "ENG", "body": "<p>x</p>"})
    assert out["ok"] is False and "no page id" in out["error"]


def test_publish_true_creates_a_current_page_and_reports_it_visible(cfg, monkeypatch):
    def handler(req):
        return {"id": "99", "title": "New", "status": "current", "space": {"key": "ENG"},
                "_links": {"webui": "/d/New"}}
    calls = _recording(monkeypatch, handler)
    out = cf.confluence_create({"title": "New", "space": "ENG", "body": "<p>x</p>",
                                "publish": True})
    assert json.loads(calls[0].data.decode())["status"] == "current"
    assert out["status"] == "current" and out["visible_in_space"] is True


def test_update_with_publish_and_no_body_publishes_the_draft_as_it_is(cfg, monkeypatch):
    def handler(req):
        if req.get_method() == "GET":
            return {"id": "99", "title": "New", "status": "draft", "version": {"number": 1},
                    "body": {"storage": {"value": "<p>keep</p>"}}}
        return {"id": "99", "title": "New", "status": "current", "_links": {"webui": "/d/New"}}
    calls = _recording(monkeypatch, handler)
    out = cf.confluence_update({"id": "99", "publish": True})
    put = [c for c in calls if c.get_method() == "PUT"][0]
    sent = json.loads(put.data.decode())
    assert sent["status"] == "current" and sent["body"]["storage"]["value"] == "<p>keep</p>"
    assert out["ok"] and out["published_now"] is True and out["status"] == "current"
