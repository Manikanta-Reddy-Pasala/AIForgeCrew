"""Post-turn learner for single (simple/plan) chat mode.

The full team pipeline runs a Learner node + memory writeback after a PASS
verdict (``pipeline.py`` wires ``make_learner_after_callback`` /
``make_consolidate_after_callback``). Simple/plan chat (``run_chat_agent``)
never did, so single-chat work only reached long-term memory if the agent
happened to call its ``memory_write`` tool itself.

This module gives the single-chat path the same distil-then-persist
behaviour: after a turn completes, summon the Learner model once over the
conversation, parse its ``facts_json`` array, and persist via
:func:`learner_persist.persist_facts`. Best-effort — never raises into chat.

Env:
  AIFORGE_CHAT_LEARNER=0            disable entirely
  AIFORGE_CHAT_LEARNER_MAX_TOKENS  learner reply cap (default 800)
  AIFORGE_CHAT_LEARNER_TIMEOUT_S   per-call timeout (default 120)
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.chat_learner")


def _disabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_LEARNER", "1") in ("0", "false", "no")


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _transcript(prompt: str, final_text: str, steps: list | None,
                limit: int = 8000) -> str:
    """Compact view of the turn — user ask, what was DONE (tool digest),
    and the assistant's answer — so the learner distils real outcomes."""
    parts = [f"USER:\n{(prompt or '').strip()}"]
    tool_lines: list[str] = []
    for s in steps or []:
        if isinstance(s, dict) and s.get("type") == "tool":
            name = s.get("name") or "tool"
            res = s.get("result")
            ok = res.get("ok") if isinstance(res, dict) else None
            tool_lines.append(f"- {name} ok={ok}")
    if tool_lines:
        parts.append("ACTIONS:\n" + "\n".join(tool_lines[:40]))
    if final_text:
        parts.append(f"ASSISTANT:\n{final_text.strip()}")
    return ("\n\n".join(parts))[:limit]


def _extract_json(raw: str) -> str:
    """Pull the JSON array out of a model reply that may wrap it in prose
    or ``` fences. Returns ``"[]"`` when nothing array-shaped is found."""
    if not raw:
        return "[]"
    t = raw.strip()
    if t.startswith("```"):
        # ```json ... ``` or ``` ... ```
        inner = t.split("```")
        if len(inner) >= 2:
            t = inner[1]
            if t.lstrip().lower().startswith("json"):
                t = t.lstrip()[4:]
    t = t.strip()
    i, j = t.find("["), t.rfind("]")
    if i != -1 and j != -1 and j > i:
        return t[i:j + 1]
    return "[]"


def learn_from_chat(*, prompt: str, final_text: str, steps: list | None,
                    repo: str, session_id, event_time: float | None = None) -> dict:
    """Distil + persist durable facts from one completed simple/plan turn.

    Soft-fails on any error (import, LLM, backend) — returns a result dict,
    never raises. Intended to run on a daemon thread off the response path.
    """
    if _disabled():
        return {"ok": False, "skipped": "disabled"}
    if not prompt or not (final_text or steps):
        return {"ok": False, "skipped": "empty"}
    repo = repo or os.environ.get("AIFORGE_AFM_REPO", "") or "repo"
    try:
        from aiforge_core.llm import client as _llm
        from aiforge_core.runtime import learner_persist, prompts
    except Exception as exc:  # noqa: BLE001
        log.debug("chat_learner import failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    messages = [
        {"role": "system", "content": prompts.LEARNER},
        {"role": "user", "content":
            "Distil durable, reusable facts from this chat turn. Output ONLY "
            "the JSON array of fact objects (each {text, about?, tags?}); use "
            "[] when nothing is worth remembering long-term. Skip pleasantries "
            "and one-off chatter.\n\n" + _transcript(prompt, final_text, steps)},
    ]
    try:
        raw = _llm.complete(
            "learner", messages,
            max_tokens=_int_env("AIFORGE_CHAT_LEARNER_MAX_TOKENS", 800),
            temperature=0.0,
            timeout_s=_int_env("AIFORGE_CHAT_LEARNER_TIMEOUT_S", 120),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("chat_learner llm failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    facts = learner_persist._coerce_facts(_extract_json(raw))
    if not facts:
        return {"ok": True, "written_observations": 0, "written_decisions": 0}
    try:
        out = learner_persist.persist_facts(
            facts=facts, repo=repo, session_id=str(session_id or ""),
            event_time=event_time)
    except Exception as exc:  # noqa: BLE001
        log.warning("chat_learner persist failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    log.info("chat_learner: repo=%s observations=%d decisions=%d",
             repo, out.get("written_observations", 0),
             out.get("written_decisions", 0))
    return {"ok": True, **out}


__all__ = ["learn_from_chat"]
