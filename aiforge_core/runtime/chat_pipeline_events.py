"""Turning the team pipeline's ADK events into chat events: text parts, streaming
deltas, planner subtasks, edit-claim guards and steer acknowledgements."""
from __future__ import annotations

import os


def _part_events(author: str, part) -> list[dict]:
    """Map a content part to the chat's existing event vocabulary
    (thought / tool) so no frontend change is needed. Each agent's
    interim text streams as a role-labelled 'thought'; the final answer
    is emitted separately as 'message' by the driver."""
    out: list[dict] = []
    text = getattr(part, "text", None)
    if text and text.strip():
        # `role` = the agent (author) so the UI can badge each step with
        # WHICH agent produced it. Text kept clean (no inline **author**).
        out.append({"type": "thought", "role": author, "text": text.strip()})
    fc = getattr(part, "function_call", None)
    if fc is not None:
        out.append({"type": "tool", "role": author,
                    "name": getattr(fc, "name", "?"),
                    "args": dict(getattr(fc, "args", None) or {}),
                    "result": {"by": author}})
    fr = getattr(part, "function_response", None)
    if fr is not None:
        resp = getattr(fr, "response", None)
        if isinstance(resp, str):
            summary = resp
        elif resp is not None:
            summary = str(resp)[:200]
        else:
            summary = ""
        out.append({"type": "thought", "role": author,
                    "text": f"{getattr(fr, 'name', '?')} → {summary}"})
    return out


def _team_streaming() -> dict:
    """RunConfig kwargs that make team agents stream their text as they write
    it (SSE partial events). ON by default: a team build is minutes of work and
    watching it arrive in one lump at the end is the worst version of it.

    This was opt-in while ``EscalatingLlm._stream_primary`` was bare — it
    skipped the stamping, retries and spend recording the buffered path had, so
    one transient 5xx ended a team agent. It now carries all of those (the
    fallback CHAIN stays deliberately unwalked mid-stream: a consumer that has
    already seen text cannot be handed a second beginning). Set
    AIFORGE_CHAT_TEAM_STREAM=0 to go back to buffered replies."""
    if os.environ.get("AIFORGE_CHAT_TEAM_STREAM", "1").strip().lower() not in (
            "1", "true", "yes", "on"):
        return {}
    try:
        from google.adk.agents.run_config import StreamingMode
        return {"streaming_mode": StreamingMode.SSE}
    except Exception:  # noqa: BLE001 — an ADK without it just does not stream
        return {}


def partial_events(event) -> list[dict]:
    """One streamed ADK chunk as 'delta' events: an agent's text is its live
    draft (muted in the UI until the finished step replaces it), reasoning is
    'thinking'. Tool-call fragments are not shown — the finished event has the
    whole call."""
    author = getattr(event, "author", None) or "agent"
    parts = getattr(getattr(event, "content", None), "parts", None) or []
    out: list[dict] = []
    for p in parts:
        text = getattr(p, "text", None)
        if text:
            out.append({"type": "delta", "role": author, "text": text,
                        "phase": "thinking" if getattr(p, "thought", False) else "draft"})
    return out


def map_event(event) -> list[dict]:
    """Map one ADK event to conversational dicts. Pure — unit-testable."""
    author = getattr(event, "author", None) or "agent"
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    out: list[dict] = []
    for p in parts:
        out.extend(_part_events(author, p))
    return out


def _event_text(event) -> str:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    return "".join(getattr(p, "text", "") or "" for p in parts).strip()


def _team_change_events(cwd: str, seq_start_sha: str, enhancer_blocked) -> list:
    """The structured Changes diff (PR-style, same events the UI renders). The
    sequential Doer edits the working tree, so include it. [] on a non-git run or
    an enhancer-blocked turn."""
    if not (seq_start_sha and not enhancer_blocked):
        return []
    try:
        from .parallel_subtasks import _emit_changes
        return list(_emit_changes(cwd, seq_start_sha, include_worktree=True))
    except Exception:  # noqa: BLE001 — never break the turn
        return []


def _guard_edit_claim(msg: str, _cwd: str, seq_start_sha: str, enhancer_blocked,
                      change_events: list) -> str:
    """The promoted answer can claim it "applied fixes" while the diff is EMPTY
    (the same hallucination the simple loop guards). When it asserts an edit but
    nothing changed, prepend an honest note. A non-git run gives no signal."""
    if not (seq_start_sha and not enhancer_blocked and not change_events):
        return msg
    try:
        from aiforge_core.runtime.chat_agent._context import (
            _claims_file_edits,
            _edit_claim_disclaimer,
            _edit_claim_guard_enabled,
        )
        if _edit_claim_guard_enabled() and _claims_file_edits(msg):
            return _edit_claim_disclaimer(msg)
    except Exception:  # noqa: BLE001 — guard must never break a turn
        pass
    return msg


def _planner_subtask_event(text: str) -> "dict | None":
    """Surface a Planner decomposition as a live subtasks event (chat is
    ticketless, so this is ephemeral). None when the plan has no subtasks."""
    try:
        from .subtasks_callback import _extract_subtickets
        subs = _extract_subtickets(text)
    except Exception:  # noqa: BLE001
        subs = []
    if not subs:
        return None
    return {"type": "subtasks", "items": [
        {"slug": s.get("slug") or f"sub-{i+1}",
         "goal": s.get("goal") or s.get("title") or "", "status": "pending"}
        for i, s in enumerate(subs)]}


def _enhancer_block_reason(ev: dict) -> "str | None":
    """The Enhancer's "too vague to act on" reason if ``ev`` is that sentinel,
    else None. The sentinel (its stand-in for a clarifying question it must never
    ask) must never reach the user as a raw thought and must STOP the run —
    otherwise it silently becomes the Planner/Doer's brief and burns minutes."""
    if ev.get("type") == "thought" and ev.get("role") == "enhancer":
        # prompts.enhancer owns the contract — including the case where the
        # Enhancer emits the line to say the request is FINE, which must not
        # stop the run.
        from aiforge_core.runtime.prompts.enhancer import block_reason
        return block_reason(ev.get("text") or "")
    return None


def _process_team_event(ev: dict, q, steps: list, by_role: dict,
                        acc: dict) -> "str | None":
    """Route one mapped team event to the queue + accumulators. Returns the
    enhancer-block reason to STOP the run, or None to continue.

    Tracks the latest substantive text PER ROLE so the final answer can be the
    Doer's work — NOT the Learner's facts JSON, which runs last and would win."""
    reason = _enhancer_block_reason(ev)
    if reason is not None:
        return reason
    q.put(ev)
    if ev.get("type") in ("thought", "tool", "error"):
        steps.append(ev)
    if ev.get("type") == "thought" and ev.get("role") and ev.get("text"):
        by_role[ev["role"]] = ev["text"]
        if ev["role"] == "planner" and not acc["emitted_subtasks"]:
            sub_ev = _planner_subtask_event(ev["text"])
            if sub_ev is not None:
                acc["emitted_subtasks"] = True
                # Keep the item-dict handle so the finally block can reconcile
                # the SAME objects (also in `steps`) to the run outcome.
                acc["sub_items"] = sub_ev["items"]
                q.put(sub_ev)
                steps.append(sub_ev)
    return None


def _fold_team_event(event, q, steps, by_role, acc):
    """Map one finished ADK event into the queue + accumulators; returns the
    Enhancer's too-vague reason when it blocked, else None."""
    blocked = None
    for ev in map_event(event):
        reason = _process_team_event(ev, q, steps, by_role, acc)
        if reason is not None:
            blocked = reason
    return blocked
