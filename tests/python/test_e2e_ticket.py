"""End-to-end DESIGN §4 TDD lifecycle — driven by Paperclip + mocked Hermes agents.

The LLM is mocked so the test is hermetic + fast. Verifies:
  - All state transitions happen in the correct order.
  - Each agent records at least one budget event + one comment.
  - Single-ticket-thread invariant (§9.1): all audit rows share one ticket_id.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from hermes.agent import Agent
from hermes.llm import LLMClient, LLMReply
from paperclip.config import PaperclipConfig
from paperclip.lifecycle import advance
from paperclip.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]


def _stub_client(content: str) -> LLMClient:
    c = MagicMock(spec=LLMClient)
    c.endpoint = "mock://"
    c.chat.return_value = LLMReply(
        content=content, reasoning="", tool_calls=[],
        usage={"prompt_tokens": 100, "completion_tokens": 50, "reasoning_tokens": 0},
    )
    return c


def test_full_tdd_lifecycle(tmp_path: Path) -> None:
    cfg = PaperclipConfig.load(REPO_ROOT)
    store = Store(tmp_path / "db.sqlite")

    # Human creates a ticket.
    t = store.create_ticket(
        title="Add validate_email() helper",
        body="src/utils.py should expose validate_email(str) → bool",
        assignee=cfg.routing.initial_assignee,
    )

    # --- EM plans ---
    em = Agent.load(REPO_ROOT, "em", model="cloud-stub",
                    client=_stub_client("Subtasks: 1) write tests; 2) implement."))
    em.run(t.id, f"Plan ticket {t.id}", store=store, cfg=cfg)
    store.add_comment(t.id, "em", "Plan complete — routing to tester")
    advance(store, cfg, t.id, "planning", actor="em")
    advance(store, cfg, t.id, "tests_writing", actor="em")

    # --- Tester writes tests ---
    tester = Agent.load(REPO_ROOT, "tester", model="glm-stub",
                        client=_stub_client("3 failing tests committed."))
    tester.run(t.id, "Write failing tests for validate_email", store=store, cfg=cfg)
    store.add_comment(t.id, "tester", "3 failing tests committed")
    advance(store, cfg, t.id, "coding", actor="tester")

    # --- Sr Dev codes ---
    dev = Agent.load(REPO_ROOT, "sr-developer", model="qwen-stub",
                     client=_stub_client("Implemented validate_email()."))
    dev.run(t.id, "Make the failing tests pass", store=store, cfg=cfg)
    store.add_comment(t.id, "sr-developer", "Implementation ready")
    advance(store, cfg, t.id, "verifying", actor="sr-developer")

    # --- Tester verifies ---
    tester.run(t.id, "Re-run tests", store=store, cfg=cfg)
    store.add_comment(t.id, "tester", "14/14 pass, 91% coverage")
    advance(store, cfg, t.id, "reviewing", actor="tester")

    # --- Architect reviews + MR ---
    arch = Agent.load(REPO_ROOT, "sr-architect", model="gemma-stub",
                      client=_stub_client("LGTM. Coverage 91%."))
    arch.run(t.id, "Review code + tests", store=store, cfg=cfg)
    store.add_comment(t.id, "sr-architect", "LGTM — creating MR")
    advance(store, cfg, t.id, "mr_created", actor="sr-architect")

    # --- Human merges ---
    advance(store, cfg, t.id, "merged", actor="human")

    final = store.get_ticket(t.id)
    assert final is not None and final.state == "merged"

    events = store.list_audit(t.id)
    # Single-ticket invariant.
    assert all(e["ticket_id"] == t.id for e in events)
    # All four agent roles recorded a budget event.
    roles_with_budget = {e["actor"] for e in events if e["event"] == "budget"}
    assert {"em", "tester", "sr-developer", "sr-architect"} <= roles_with_budget
    # Lifecycle moved through the full chain.
    transitions = [e["data"]["to"] for e in events if e["event"] == "transition"]
    assert transitions == [
        "planning", "tests_writing", "coding", "verifying", "reviewing", "mr_created", "merged"
    ]
    # Each role commented at least once.
    authors = {c.author for c in store.list_comments(t.id)}
    assert {"em", "tester", "sr-developer", "sr-architect"} <= authors
