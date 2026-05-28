"""Tests for lightweight PR-comment classification/routing (gap A4)."""
from __future__ import annotations

import importlib

import pytest

mod = importlib.import_module("aiforge_core.runtime.pr_comments_loop")


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Why is this null-checked here?", "question"),
        ("what does this function return", "question"),
        ("Can you clarify the intent", "question"),
        ("Should we handle the empty case?", "question"),
        ("Is this thread-safe", "question"),
        ("nit: rename this variable", "nit"),
        ("Tiny typo in the docstring", "nit"),
        ("please fix the lint/style here", "nit"),
        ("Please refactor this to use the new API and add a test", "change_request"),
        ("This will break on negative input, handle it.", "change_request"),
    ],
)
def test_classify_comment(body, expected):
    assert mod.classify_comment(body) == expected


def test_classify_empty_is_change_request():
    assert mod.classify_comment("") == "change_request"
    assert mod.classify_comment("   ") == "change_request"


def test_route_question_is_lightweight():
    out = mod.route_comment({"id": 11, "body": "What is this for?"})
    assert out["mode"] == "lightweight"
    assert out["comment_id"] == 11
    assert "question" in out["reason"]


def test_route_nit_is_lightweight():
    out = mod.route_comment({"id": 12, "body": "nit: spelling"})
    assert out["mode"] == "lightweight"
    assert out["comment_id"] == 12


def test_route_change_request_is_full():
    out = mod.route_comment({"id": 13, "body": "Refactor the loop and add tests"})
    assert out["mode"] == "full"
    assert out["comment_id"] == 13


def test_flag_zero_forces_full(monkeypatch):
    monkeypatch.setenv("AIFORGE_PR_COMMENT_LIGHTWEIGHT", "0")
    out = mod.route_comment({"id": 14, "body": "What is this for?"})
    assert out["mode"] == "full"
    assert "lightweight_disabled" in out["reason"]


def test_flag_default_enables_lightweight(monkeypatch):
    monkeypatch.delenv("AIFORGE_PR_COMMENT_LIGHTWEIGHT", raising=False)
    out = mod.route_comment({"id": 15, "body": "Is this correct?"})
    assert out["mode"] == "lightweight"


def test_lightweight_reply_stub_returns_text():
    reply = mod.lightweight_reply({"id": 16, "body": "What is this for?"})
    assert reply["comment_id"] == 16
    assert isinstance(reply["reply_text"], str)
    assert reply["reply_text"]
    assert reply["posted"] is False
