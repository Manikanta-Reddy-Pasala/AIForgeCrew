from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes.agent import Agent
from hermes.llm import LLMClient, LLMReply
from aiforge_core.config import PaperclipConfig
from aiforge_core.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]


def _mk_client(replies: list[LLMReply]) -> LLMClient:
    c = MagicMock(spec=LLMClient)
    c.endpoint = "mock://"
    c.chat.side_effect = replies
    return c


def test_simple_no_tool_call(tmp_path: Path) -> None:
    client = _mk_client([LLMReply(content="hello world", reasoning="", tool_calls=[],
                                  usage={"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 0})])
    a = Agent.load(REPO_ROOT, "sr-developer", model="stub", client=client)
    reply = a.run(ticket_id=None, user_message="say hi")
    assert reply.content == "hello world"
    assert client.chat.call_count == 1


def test_tool_call_round(tmp_path: Path) -> None:
    # Round 1: model asks to read a file. Round 2: model returns final answer.
    tc = [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": "README.md"})},
    }]
    r1 = LLMReply(content="", reasoning="", tool_calls=tc,
                  usage={"prompt_tokens": 50, "completion_tokens": 20, "reasoning_tokens": 0})
    r2 = LLMReply(content="Read it.", reasoning="", tool_calls=[],
                  usage={"prompt_tokens": 80, "completion_tokens": 10, "reasoning_tokens": 0})
    client = _mk_client([r1, r2])
    a = Agent.load(REPO_ROOT, "sr-developer", model="stub", client=client)
    reply = a.run(ticket_id=None, user_message="read the readme")
    assert reply.content == "Read it."
    assert client.chat.call_count == 2


def test_budget_integration(tmp_path: Path) -> None:
    # Very small usage that still stays within sr_developer 150_000-token cap.
    r = LLMReply(content="done", reasoning="", tool_calls=[],
                 usage={"prompt_tokens": 100, "completion_tokens": 20, "reasoning_tokens": 0})
    client = _mk_client([r])
    cfg = PaperclipConfig.load(REPO_ROOT)
    store = Store(tmp_path / "db.sqlite")
    t = store.create_ticket("x", "", assignee="sr_developer")
    a = Agent.load(REPO_ROOT, "sr-developer", model="stub", client=client)
    a.run(ticket_id=t.id, user_message="do", store=store, cfg=cfg)
    # Budget spend recorded in audit.
    events = [e for e in store.list_audit(t.id) if e["event"] == "budget"]
    assert len(events) == 1
    assert events[0]["data"]["tokens"] == 120
