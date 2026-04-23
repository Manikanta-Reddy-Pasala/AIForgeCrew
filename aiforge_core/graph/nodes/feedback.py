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
    # Qwen3.6 is a reasoning model. LM Studio exposes a chat_template_kwarg
    # (``enable_thinking: false``) that routes the output into ``content``
    # directly instead of burning the budget on ``reasoning_content``. The
    # ``/no_think`` in-prompt toggle does NOT work for Qwen3.6 — verified
    # 2026-04-24 on both qwen3.6-27b and qwen3.6-35b-a3b.
    # We keep max_tokens high (2048) and fall back to reasoning_content as a
    # defensive last resort.
    payload = json.dumps({
        "model": FEEDBACK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
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
    # Reasoning-model fallback: some runtimes put the answer in
    # `reasoning_content` when /no_think is ignored.
    return (msg.get("reasoning_content") or "").strip()


def _parse_verdict(text: str) -> dict:
    """Parse the LLM's verdict response.

    Tolerate markdown fences, leading prose/CoT, and JSON with trailing
    commentary. Falls back to regex-matching ``verdict: <value>`` and
    ``reason: <...>`` lines so a non-JSON reply still yields actionable
    feedback instead of ``could not parse verdict JSON``.
    """
    if not text:
        return {"verdict": "fail", "reason": "empty verdict", "fixlist": []}
    raw = text.strip()
    # Drop first/last triple-backtick fence if present.
    if raw.startswith("```"):
        fenced = raw.split("```")
        if len(fenced) >= 3:
            raw = fenced[1].lstrip("json").lstrip("JSON").strip()

    # First try: the outermost balanced {...} block anywhere in the text.
    # Scan brace depth to find the first complete object (handles trailing prose).
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(raw[start:i + 1])
                    if isinstance(obj, dict) and "verdict" in obj:
                        obj.setdefault("reason", "")
                        obj.setdefault("fixlist", [])
                        return obj
                except Exception:
                    pass
                start = -1

    # Fallback: regex verdict/reason out of plain text.
    import re
    vm = re.search(r"verdict\s*[:=]\s*\"?(pass|fail|scope_violation)", raw, re.I)
    if vm:
        verdict = vm.group(1).lower()
        rm = re.search(r"reason\s*[:=]\s*\"?([^\"\n]{3,300})", raw, re.I)
        reason = (rm.group(1).strip().rstrip("\",.") if rm else
                  raw[:300].replace("\n", " "))
        return {"verdict": verdict, "reason": reason, "fixlist": []}

    return {"verdict": "fail",
            "reason": f"could not parse verdict JSON (got: {raw[:140]!r})",
            "fixlist": []}


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
