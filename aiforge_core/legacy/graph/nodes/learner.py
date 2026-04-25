"""Learner node — single-shot insight extraction.

Previous legacy path ran _run_tool_loop which looped on qwen-coder.
Replaced with one LLM call that produces a short DIGEST line and one
T1 memory write.
"""
from __future__ import annotations

import json
import time
import urllib.request

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.config import (
    LEARNER_MODEL,
    LM_STUDIO_API_KEY,
    LM_STUDIO_BASE_URL,
)
from aiforge_core.runtime.logging_setup import emit, get_logger
from aiforge_core.runtime.orchestrator import _write_t1_memory

from ..state import AgentState


LEARNER_PROMPT = """You are the Learner. Extract one single DIGEST line from the work done on this ticket.

## Ticket
{body}

## Most recent events
{events}

Respond with a JSON object ONLY. No prose before or after.
Keys:
- digest: one short line (<= 200 chars) summarizing what was learned / shipped.
- keywords: array of up to 5 short keywords.

Your JSON:
"""


def _recent_events_text(ticket_id: int, limit: int = 6) -> str:
    events = tickets_mod.comments(ticket_id, limit=limit)
    if not events:
        return "(no prior events)"
    lines = []
    for e in events:
        kind = e.get("kind") or "?"
        role = e.get("agent_role") or "?"
        body = (e.get("body") or "").replace("\n", " ")[:300]
        lines.append(f"[{role}] ({kind}) {body}")
    return "\n".join(lines)


def _call_llm(prompt: str) -> str:
    # Qwen3.6 is a reasoning model. `enable_thinking=false` routes the
    # answer into `content` instead of burning the budget on
    # `reasoning_content`. Large max_tokens is a defensive cap — LM Studio
    # returns 400 if the prompt + expected output exceeds context.
    payload = json.dumps({
        "model": LEARNER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 524288,
        "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"{LM_STUDIO_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LM_STUDIO_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read())
    msg = (body.get("choices") or [{}])[0].get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    if content:
        return content
    return (msg.get("reasoning_content") or "").strip()


def _parse(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1]) if text.count("```") >= 2 else text.strip("`")
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return {"digest": text[:200], "keywords": []}


def learner_node(state: AgentState) -> AgentState:
    ticket_id = state["ticket_id"]
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        return {**state, "stop_reason": "blocked"}

    log = get_logger("learner")
    t0 = time.time()

    body = (ticket.body or "")[:4000]
    events_text = _recent_events_text(ticket_id)[:4000]
    prompt = LEARNER_PROMPT.format(body=body, events=events_text)

    try:
        raw = _call_llm(prompt)
        parsed = _parse(raw)
    except Exception as exc:
        parsed = {"digest": f"learner llm error: {exc}", "keywords": []}
        emit(log, "learner.llm_error", error=str(exc)[:200])

    digest = (parsed.get("digest") or "")[:200] or "(empty)"
    keywords = parsed.get("keywords") or []

    summary = {
        "stop_reason": "done",
        "has_commented": True,
        "turns": 1,
        "wall_s": round(time.time() - t0, 2),
        "digest": digest,
    }

    try:
        _write_t1_memory(ticket, "learner", summary, log)
    except Exception as exc:
        emit(log, "learner.t1_write_error", error=str(exc)[:200])

    tickets_mod.add_event(
        ticket_id, "learner", "comment",
        body=f"DIGEST: {digest}\nkeywords: {', '.join(keywords[:5])}",
        metadata={"source": "learner_single_shot"},
    )

    fresh = tickets_mod.get(ticket_id)
    updated_ticket = dict(fresh.__dict__) if fresh else state["ticket"]

    return {
        **state,
        "role": "learner",
        "ticket": updated_ticket,
        "stop_reason": "done",
        "learner_digest": digest,
        "tool_results": state.get("tool_results", []) + [summary],
    }
