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


def _changed_files(steps: list | None) -> list[str]:
    """Repo-mutating tool calls that SUCCEEDED → the paths they touched.
    Ground truth that the turn changed the repo — used to author a solution
    deterministically instead of trusting the model to self-declare a DID:
    record (local models routinely emit a generic 'structure' fact instead)."""
    from aiforge_core.runtime.text_doer import _EDIT_TOOLS
    paths: list[str] = []
    for s in steps or []:
        if not isinstance(s, dict) or s.get("type") != "tool":
            continue
        if (s.get("name") or "").lower() not in _EDIT_TOOLS:
            continue
        res = s.get("result")
        if not (isinstance(res, dict) and res.get("ok")):
            continue
        pth = (s.get("args") or {}).get("path") or res.get("path")
        if pth:
            paths.append(str(pth))
    return list(dict.fromkeys(paths))          # dedupe, keep order


def _is_solution_fact(f: dict) -> bool:
    """Mirror of learner_persist._is_sol — does this fact already declare a
    completed feature/fix (so we should NOT synthesize a second one)?"""
    if not isinstance(f, dict):
        return False
    txt = (f.get("text") or "").strip()
    return (str(f.get("kind") or "").lower() in ("feature", "fix")
            or txt.upper().startswith("DID:")
            or f.get("topic") == "task-history")


def _synthesize_solution_fact(prompt: str, facts: list, changed: list[str]) -> dict:
    """Build the task-done record from GROUND TRUTH (files actually changed).
    Summary prefers the model's first distilled fact (it describes what was
    done) and falls back to the user request. kind='fix' when the ask reads
    like a bug fix, else 'feature'. ``about`` = the changed file basenames so
    the OKF solution links to them."""
    import os as _os
    import re as _re
    summary = ""
    for f in facts:
        if isinstance(f, dict) and (f.get("text") or "").strip():
            summary = f["text"].strip()
            break
    if not summary:
        summary = (prompt or "").strip()
    kind = "fix" if _re.search(r"\b(fix|bug|error|broken|regress|crash|fail)",
                               (prompt or "").lower()) else "feature"
    return {"text": "DID: " + summary[:180], "topic": "task-history",
            "kind": kind, "about": [_os.path.basename(p) for p in changed][:8],
            "files": changed[:20]}


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
            "the JSON array of fact objects, following the schema in the system "
            "prompt: each fact is {text, topic, about?, tags?}. IMPORTANT — when "
            "this turn actually CHANGED the repo (created/edited/fixed files), "
            "ALSO emit the task-done record: {text:'DID: <what was asked + what "
            "changed>', topic:'task-history', kind:'feature'|'fix', tables?:[…], "
            "services?:[…], about?:[files/symbols]} — this is what builds the "
            "solution changelog, so never omit it on a real change. Use [] when "
            "nothing is worth remembering long-term. Skip pleasantries and "
            "one-off chatter.\n\n" + _transcript(prompt, final_text, steps)},
    ]
    _llm_down = False
    try:
        raw = _llm.complete(
            "learner", messages,
            max_tokens=_int_env("AIFORGE_CHAT_LEARNER_MAX_TOKENS", 800),
            temperature=0.0,
            timeout_s=_int_env("AIFORGE_CHAT_LEARNER_TIMEOUT_S", 120),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("chat_learner llm failed: %s", exc)
        raw, _llm_down = "", True
        # DETERMINISTIC FALLBACK: when the distil LLM is down (flaky endpoint), an
        # EXPLICIT instruction to remember must still persist — else a stated
        # preference is silently lost. Save the raw user line as one fact.
        from aiforge_core.runtime.capture_cues import has_cue as _has_cue
        if _has_cue(prompt):
            try:
                learner_persist.persist_facts(
                    facts=[{"text": prompt.strip()[:500], "tags": ["preference"]}],
                    repo=repo, session_id=str(session_id or ""),
                    event_time=event_time)
                log.info("chat_learner fallback saved raw instruction (LLM down)")
            except Exception:  # noqa: BLE001
                pass

    facts = learner_persist._coerce_facts(_extract_json(raw))
    # GROUND-TRUTH SOLUTION: if this turn actually changed the repo (edit tools
    # succeeded) and the model didn't self-declare a task-done record, author
    # one from ground truth — so a completed feature/fix ALWAYS lands in the OKR
    # changelog, even when the distiller emitted a generic fact or was down.
    changed = _changed_files(steps)
    if changed and not any(_is_solution_fact(f) for f in facts):
        facts.append(_synthesize_solution_fact(prompt, facts, changed))
        log.info("chat_learner: synthesized DID solution from %d changed file(s)",
                 len(changed))
    if not facts:
        return {"ok": True, "written_observations": 0, "written_decisions": 0,
                "llm_down": _llm_down}
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
