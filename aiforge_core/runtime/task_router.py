"""LLM task-type classifier for chat routing.

The regex heuristics in the API producer (``_looks_like_multifile_build`` /
``_is_doc_analysis_task``) are fast but brittle: "create 2 JIRA tickets about
the API" reads as a code build because it names "api". This module runs ONE
cheap classify (the triage/fast model) to bucket the request into a routing
category, and the producer uses it to pick the path — falling back to the regex
when the LLM is disabled, times out, or answers ambiguously (so routing never
depends on the model being up).

Categories:
  chat          a question / explanation / conversation → answer directly
  tracker       create/update a JIRA ticket|story|epic|issue or Confluence page
                → single agent with jira_create/confluence_create (NOT a build)
  doc_analysis  produce a document/report/analysis/summary/review (prose)
                → the read-only research agent
  code_build    build a NEW multi-file app/service/module/feature from scratch
                → the decompose→scaffold→implement→test pipeline
  code_edit     a small/targeted change to existing code → single agent

Env:
  AIFORGE_LLM_TASK_ROUTER=0        disable (always use the regex fallback)
  AIFORGE_TASK_ROUTE_ROLE=triage   model role for the classify
  AIFORGE_TASK_ROUTE_TIMEOUT_S=15  per-call budget
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.task_router")

CATEGORIES = ("chat", "tracker", "doc_analysis", "code_build", "code_edit")

_SYS = (
    "You are a routing classifier for a coding assistant. Read the user's "
    "request and output ONE word for how it should be handled:\n"
    "- CHAT: a question, explanation, advice, or conversation — answer it "
    "directly, no files.\n"
    "- TRACKER: create or update a JIRA ticket/story/epic/issue or a Confluence "
    "page (a tracker/wiki WRITE). Mentioning an api/service is the ticket's "
    "SUBJECT, not code to build.\n"
    "- DOC: produce a document, report, analysis, summary, or review — the "
    "deliverable is prose, not code.\n"
    "- BUILD: build a NEW multi-file app, service, module, or feature from "
    "scratch (needs planning + several files).\n"
    "- EDIT: a small, targeted change to EXISTING code (edit/rename/fix in a "
    "few spots).\n"
    "Answer with EXACTLY one word: CHAT, TRACKER, DOC, BUILD, or EDIT."
)

_WORD_TO_CAT = {
    "chat": "chat", "tracker": "tracker", "doc": "doc_analysis",
    "build": "code_build", "edit": "code_edit",
}


def _disabled() -> bool:
    return os.environ.get("AIFORGE_LLM_TASK_ROUTER", "1") in ("0", "false", "no")


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _recent_context(history, n: int = 3) -> str:
    out: list[str] = []
    for m in (history or [])[-n:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        txt = (m.get("content") or "").strip().replace("\n", " ")
        if txt:
            out.append(f"{role}: {txt[:200]}")
    return "\n".join(out)


def classify_task(prompt: str, history=None, cwd: str | None = None) -> "str | None":
    """Return one of :data:`CATEGORIES`, or ``None`` when the classifier is
    disabled / errored / ambiguous (caller then uses its regex fallback so
    routing never hard-depends on the model). Deterministic trivial/greeting →
    ``chat`` without an LLM call."""
    p = (prompt or "").strip()
    if not p or _disabled():
        return None
    # Deterministic short-circuit for greetings / acks / tiny fragments.
    try:
        from aiforge_core.runtime.parallel_subtasks import _is_trivial_prompt
        if _is_trivial_prompt(p):
            return "chat"
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.llm import client as _llm
    except Exception as exc:  # noqa: BLE001
        log.debug("task_router import failed: %s", exc)
        return None
    role = os.environ.get("AIFORGE_TASK_ROUTE_ROLE", "triage").strip() or "triage"
    ctx = _recent_context(history)
    user = (f"Recent conversation:\n{ctx}\n\n" if ctx else "") + \
        f"Request:\n{p}\n\nOne word — CHAT, TRACKER, DOC, BUILD, or EDIT?"
    try:
        raw = _llm.complete(
            role, [{"role": "system", "content": _SYS},
                   {"role": "user", "content": user}],
            max_tokens=8, temperature=0.0,
            timeout_s=_int_env("AIFORGE_TASK_ROUTE_TIMEOUT_S", 15),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("task_router classify failed: %s", exc)
        return None
    low = (raw or "").strip().lower()
    # First recognised keyword wins (tolerate a stray leading token / punctuation).
    for word, cat in _WORD_TO_CAT.items():
        if word in low:
            return cat
    return None


__all__ = ["classify_task", "CATEGORIES"]
