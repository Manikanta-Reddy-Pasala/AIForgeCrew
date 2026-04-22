"""Feedback node — single-shot verdict on Doer's diff.

The legacy tool-loop path tended to spin on identical `git diff` calls
with qwen-coder. This replacement inspects the worktree diff directly
and issues ONE LLM call for a pass/fail verdict with reasons.
"""
from __future__ import annotations

import json
import subprocess
import time

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.config import (
    LM_STUDIO_API_KEY,
    LM_STUDIO_BASE_URL,
    FEEDBACK_MODEL,
)
from aiforge_core.runtime.logging_setup import get_logger

from ..state import AgentState


FEEDBACK_PROMPT = """You are the Feedback agent. Judge whether the Doer's diff implements the ticket.

## Ticket
{body}

## Diff (git diff HEAD~1)
```
{diff}
```

Rules:
- Respond with a JSON object ONLY. No prose before or after.
- Keys: verdict (one of "pass"|"fail"|"scope_violation"), reason (≤ 200 chars), fixlist (optional, array of ≤ 5 short strings).
- "pass" when the diff implements the acceptance criteria AND compile was green.
- "fail" when the diff misses a criterion or introduces a bug. Populate fixlist with specific asks.
- "scope_violation" when the diff touches files outside the ## Files allowlist.

Your JSON:
"""


def _git_diff(worktree_path: str | None) -> str:
    if not worktree_path:
        return ""
    try:
        proc = subprocess.run(
            ["git", "diff", "HEAD~1"],
            cwd=worktree_path, capture_output=True, text=True, timeout=30, check=False,
        )
        return (proc.stdout or proc.stderr)[:15000]
    except Exception as exc:
        return f"(git diff failed: {exc})"


def _call_llm(prompt: str) -> str:
    import urllib.request
    payload = json.dumps({
        "model": FEEDBACK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
        "temperature": 0.0,
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
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    return (body.get("choices") or [{}])[0].get("message", {}).get("content", "")


def _parse_verdict(text: str) -> dict:
    # Strip markdown fences and extract first {...} block.
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
    return {"verdict": "fail", "reason": "could not parse verdict JSON", "fixlist": []}


def feedback_node(state: AgentState) -> AgentState:
    ticket_id = state["ticket_id"]
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        return {**state, "stop_reason": "blocked"}

    log = get_logger("feedback")
    worktree = state.get("worktree_path")

    t0 = time.time()
    diff = _git_diff(worktree)
    body = (ticket.body or "")[:8000]
    prompt = FEEDBACK_PROMPT.format(body=body, diff=diff[:12000])

    try:
        raw = _call_llm(prompt)
        verdict_obj = _parse_verdict(raw)
    except Exception as exc:
        verdict_obj = {"verdict": "fail", "reason": f"llm error: {exc}", "fixlist": []}

    verdict = verdict_obj.get("verdict") or "fail"
    if verdict not in ("pass", "fail", "scope_violation"):
        verdict = "fail"
    reason = (verdict_obj.get("reason") or "")[:500]
    fixlist = verdict_obj.get("fixlist") or []
    fixlist_str = "\n".join(f"- {x}" for x in fixlist[:5]) if fixlist else ""

    tickets_mod.add_event(
        ticket_id, "feedback", "comment",
        body=f"verdict={verdict}\nreason={reason}\n{fixlist_str}",
        metadata={"feedback_verdict": verdict, "feedback_reason": reason},
    )

    summary = {
        "stop_reason": "verdict",
        "has_commented": True,
        "turns": 1,
        "wall_s": round(time.time() - t0, 2),
        "verdict": verdict,
        "reason": reason,
    }

    fresh = tickets_mod.get(ticket_id)
    updated_ticket = dict(fresh.__dict__) if fresh else state["ticket"]

    fail_count = state.get("feedback_fail_count") or 0
    if verdict == "fail":
        fail_count += 1

    return {
        **state,
        "role": "feedback",
        "ticket": updated_ticket,
        "worktree_path": worktree,
        "stop_reason": summary.get("stop_reason"),
        "verdict": verdict,
        "feedback_fixlist": fixlist_str or None,
        "feedback_fail_count": fail_count,
        "tool_results": state.get("tool_results", []) + [summary],
    }
