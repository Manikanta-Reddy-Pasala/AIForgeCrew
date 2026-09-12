"""Author a skill or workflow from a chat turn that earned one.

Rules already capture themselves — ``rule_capture`` runs after every turn, so a
correction becomes a rule without anyone asking. Skills and workflows did not:
they existed only when the AGENT remembered to call ``learn_skill`` /
``learn_workflow``. The library therefore grew for corrections and never for
procedures, and "how we did that last time" is the thing a user most often
wants back.

This is that missing path, and it is deliberately stingy — a library full of
near-duplicates is worse than a small one:

  * Only turns that DID something are considered: a few tool steps, or an
    explicit "save this as a skill/workflow". A question-and-answer turn writes
    nothing and costs no model call.
  * ONE structured call on the ``learner`` role (the unattended maintenance
    role), so it sits under the operator's rate ceiling and shows up in the
    request meter like every other background sender.
  * It DEDUPES BEFORE IT WRITES, using the same similarity the nightly sweep
    uses (:mod:`artifact_merge`). Not writing a duplicate is cheaper than
    merging one afterwards, and one definition of "similar" means the admission
    check and the sweep can never drift apart.

Switches:
  AIFORGE_ARTIFACT_CAPTURE=0        turn it off
  AIFORGE_ARTIFACT_CAPTURE_MIN_STEPS=3   tool steps that count as "did work"
  AIFORGE_ARTIFACT_CAPTURE_DEDUPE=0.72   similarity at which we skip writing
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("aiforge.artifact_capture")

_KINDS = ("skill", "workflow")

# "save this as a workflow" — an explicit ask always qualifies, however small
# the turn was.
_EXPLICIT_RE = re.compile(
    r"\b(save|remember|capture|keep|turn\s+(?:this|that)\s+into|make)\b"
    r"[^.\n]{0,60}?\b(skill|workflow|playbook|procedure|runbook)\b", re.I)

_SYSTEM = (
    "You decide whether a finished chat turn contains a REUSABLE procedure "
    "worth saving to the library, and write it if so.\n"
    "\n"
    "Answer with kind='none' unless the turn actually established a repeatable "
    "way of doing something. Most turns do not: a question answered, a file "
    "read, a one-off edit, an explanation — all are kind='none'. Saving those "
    "fills the library with noise that is injected into later prompts.\n"
    "\n"
    "kind='skill'    a reusable technique or approach: how to diagnose X, how "
    "to write Y in this codebase. Short, and about JUDGEMENT.\n"
    "kind='workflow' an end-to-end procedure with ORDERED steps someone could "
    "follow again: how we cut a release, how we onboard an integration.\n"
    "\n"
    "When you do write one: name it for what it achieves (not for this "
    "session), give a one-line description, list the phrases that should bring "
    "it back (triggers), and write a body that a future agent can FOLLOW — "
    "concrete commands, paths and checks from this turn, not a summary of what "
    "happened. Never invent steps that were not taken."
)


def disabled() -> bool:
    return os.environ.get("AIFORGE_ARTIFACT_CAPTURE", "1").strip().lower() in (
        "0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _tool_steps(steps) -> int:
    return sum(1 for s in (steps or [])
               if isinstance(s, dict) and s.get("type") == "tool")


def worth_capturing(prompt: str, steps) -> bool:
    """Whether this turn is even a candidate — checked BEFORE any model call."""
    if _EXPLICIT_RE.search(prompt or ""):
        return True
    return _tool_steps(steps) >= _env_int(
        "AIFORGE_ARTIFACT_CAPTURE_MIN_STEPS", 3)


def _proposal_model():
    """Lazy: pydantic is a heavy import for a module that mostly declines."""
    from pydantic import BaseModel, Field

    class Proposal(BaseModel):
        kind: str = Field(default="none",
                          description="skill | workflow | none")
        name: str = Field(default="", description="what it achieves")
        description: str = Field(default="", description="one line")
        triggers: list[str] = Field(default_factory=list)
        body: str = Field(default="", description="steps a future agent follows")

    return Proposal


def _turn_digest(prompt: str, final_text: str, steps) -> str:
    """What the model reads: the ask, what was done, and the answer."""
    did = []
    for s in (steps or [])[:40]:
        if not isinstance(s, dict):
            continue
        if s.get("type") == "tool":
            did.append(f"- {s.get('name') or 'tool'}: "
                       f"{str(s.get('text') or '')[:160]}")
    body = "\n".join(did[:25]) or "(no tool steps)"
    return (f"USER ASKED:\n{(prompt or '')[:2000]}\n\n"
            f"WHAT WAS DONE:\n{body}\n\n"
            f"FINAL ANSWER:\n{(final_text or '')[:2000]}")


def _propose(prompt: str, final_text: str, steps):
    from aiforge_core.llm.structured import structured_complete
    return structured_complete(
        "learner",
        [{"role": "system", "content": _SYSTEM},
         {"role": "user", "content": _turn_digest(prompt, final_text, steps)}],
        _proposal_model(), max_tokens=1400, temperature=0.0)


def existing_match(kind: str, name: str, description: str, triggers,
                   body: str) -> str:
    """The name of an artifact we already have that says this, or ``""``."""
    from aiforge_core.runtime import artifact_merge as _am
    plural = kind + "s"
    cand = _am.item_from(plural, name, description, triggers, body)
    floor = _env_float("AIFORGE_ARTIFACT_CAPTURE_DEDUPE", 0.72)
    best, who = 0.0, ""
    for item in _am.load(plural):
        score = _am.similarity(cand, item)
        if score > best:
            best, who = score, item.name
    return who if best >= floor else ""


def _write(kind: str, prop, cwd: str) -> dict:
    if kind == "skill":
        from aiforge_core.runtime import skills
        return skills.write_skill(
            name=prop.name, description=prop.description, body=prop.body,
            triggers=list(prop.triggers or []), cwd=cwd, scope="global")
    from aiforge_core.runtime import workflows
    return workflows.write_workflow(
        name=prop.name, description=prop.description, body=prop.body,
        triggers=list(prop.triggers or []), cwd=cwd, scope="global")


def capture_from_chat(*, prompt: str, final_text: str, steps, cwd: str,
                      session_id=None) -> dict:
    """Save a skill/workflow this turn earned. Never raises; returns a report.

    Runs on the post-turn daemon thread beside ``preference_capture`` and
    ``chat_learner``, so nothing here is on the response path."""
    if disabled():
        return {"ok": False, "captured": False, "skipped": "disabled"}
    if not worth_capturing(prompt, steps):
        return {"ok": True, "captured": False, "skipped": "not a procedure"}
    try:
        prop = _propose(prompt, final_text, steps)
    except Exception as exc:  # noqa: BLE001 — a model outage is not a failure
        log.debug("artifact_capture: proposal failed: %s", exc)
        return {"ok": False, "captured": False, "error": str(exc)[:200]}

    kind = (getattr(prop, "kind", "") or "none").strip().lower().rstrip("s")
    name = (getattr(prop, "name", "") or "").strip()
    body = (getattr(prop, "body", "") or "").strip()
    if kind not in _KINDS or not name or not body:
        return {"ok": True, "captured": False, "skipped": "nothing reusable"}

    dupe = existing_match(kind, name, prop.description, prop.triggers, body)
    if dupe:
        # The sweep would only have to merge it back out again.
        log.info("artifact_capture: %s '%s' already covered by '%s' — not saved",
                 kind, name, dupe)
        return {"ok": True, "captured": False, "skipped": f"duplicate of {dupe}",
                "kind": kind, "name": name, "duplicate_of": dupe}

    res = _write(kind, prop, cwd)
    if not (isinstance(res, dict) and res.get("ok")):
        return {"ok": False, "captured": False, "kind": kind, "name": name,
                "error": (res or {}).get("error", "write failed")}
    log.info("artifact_capture: saved %s '%s' from session %s",
             kind, name, session_id)
    return {"ok": True, "captured": True, "kind": kind, "name": name,
            "path": res.get("path", "")}


__all__ = ["capture_from_chat", "disabled", "existing_match", "worth_capturing"]
