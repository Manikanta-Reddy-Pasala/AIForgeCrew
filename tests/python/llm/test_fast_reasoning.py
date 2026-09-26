"""A fast role asks the server to skip reasoning (``reasoning_effort``), and a
server that refuses the field is remembered and re-sent without it."""
from __future__ import annotations

import io
import json
import types
import urllib.error

import pytest

from aiforge_core.llm import client as c
from aiforge_core.llm import fast_reasoning


def _ep(url="http://srv/v1"):
    return types.SimpleNamespace(model="m", provider="test", extras={},
                                 base_url=url)


def _body(content):
    return {"choices": [{"message": {"content": content}}]}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    fast_reasoning.reset()
    monkeypatch.delenv("AIFORGE_FAST_ROLE_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AIFORGE_FAST_ROLE_NO_THINK", raising=False)
    monkeypatch.setattr(c, "_record_usage", lambda *a, **k: None)
    yield
    fast_reasoning.reset()


def _call(role, ep=None):
    return c._try_post(ep or _ep(), [{"role": "user", "content": "q"}],
                       temperature=0.0, max_tokens=8, top_p=None, extras=None,
                       timeout_s=5, role=role, source="primary")


def test_fast_role_sends_reasoning_effort_none(monkeypatch):
    posts = []
    monkeypatch.setattr(c, "_post_with_retry", lambda ep, p, t, **k: (
        posts.append(json.loads(p)), _body("DOC"))[1])
    assert _call("triage")[0] == "DOC"
    assert posts[0]["reasoning_effort"] == "none"


def test_other_roles_do_not_send_it(monkeypatch):
    posts = []
    monkeypatch.setattr(c, "_post_with_retry", lambda ep, p, t, **k: (
        posts.append(json.loads(p)), _body("answer"))[1])
    _call("chat")
    assert "reasoning_effort" not in posts[0]


def test_env_off_disables(monkeypatch):
    monkeypatch.setenv("AIFORGE_FAST_ROLE_REASONING_EFFORT", "off")
    posts = []
    monkeypatch.setattr(c, "_post_with_retry", lambda ep, p, t, **k: (
        posts.append(json.loads(p)), _body("x1"))[1])
    _call("triage")
    assert "reasoning_effort" not in posts[0]


def _refusal():
    return urllib.error.HTTPError(
        "http://srv/v1/chat/completions", 400, "Bad Request", {},
        io.BytesIO(b'{"error": "Unrecognized request argument: reasoning_effort"}'))


def test_refused_field_is_dropped_and_remembered(monkeypatch):
    posts = []

    def _post(ep, p, t, **k):
        body = json.loads(p)
        posts.append(body)
        if "reasoning_effort" in body:
            raise _refusal()
        return _body("DOC")
    monkeypatch.setattr(c, "_post_with_retry", _post)
    assert _call("triage")[0] == "DOC"
    assert len(posts) == 2 and "reasoning_effort" not in posts[1]
    # Next call to the same server: no failed round trip first.
    _call("triage")
    assert len(posts) == 3 and "reasoning_effort" not in posts[2]
    # Another server still gets the field.
    _call("triage", _ep("http://other/v1"))
    assert posts[3]["reasoning_effort"] == "none"


def test_unrelated_400_is_not_retried(monkeypatch):
    posts = []

    def _post(ep, p, t, **k):
        posts.append(1)
        raise urllib.error.HTTPError("u", 400, "Bad", {},
                                     io.BytesIO(b'{"error": "context too long"}'))
    monkeypatch.setattr(c, "_post_with_retry", _post)
    assert _call("triage") is None
    assert len(posts) == 1
