"""Chat task-type routing decision — pure + unit-testable.

Extracted from the api.py chat handler (it was ~120 lines of nested closures +
deep conditionals inside a streaming generator, so it could not be tested in
isolation). This module makes ONE decision: given a request + the run's context
(mode, parallel capability, greenfield, classifier verdict, approvals), which
path handles it —

  doc_analysis → research agent · code_build → build pipeline ·
  tracker/chat/code_edit → single chat agent.

The LLM classify (task_router.classify_task) and the streaming dispatch stay in
the caller; this module is a pure function of already-gathered inputs, so the
whole decision is testable without spinning an agent.

The two regex predicates are SAFETY NETS, not the classifier:
  * is_advice_question — a QUESTION never auto-launches a file-writing pipeline
    even if the classifier misfires.
  * regex_build_fallback — used only when there's no positive classification
    (follow-up / classifier disabled or errored) so a real build still escalates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_ADVICE_RE = re.compile(
    r"^(how|what|why|where|when|which|who|should|can|could|would|"
    r"is|are|do|does|did|explain|tell me|show me|help me understand|"
    r"any (idea|thought)|best way)\b", re.IGNORECASE)
_TRACKER_PLATFORM_RE = re.compile(r"\b(jira|confluence)\b", re.IGNORECASE)
_TRACKER_ITEM_RE = re.compile(
    r"\b(ticket|tickets|story|stories|epic|epics|issue|issues|page|pages)\b",
    re.IGNORECASE)
_BUILD_VERB_RE = re.compile(
    r"\b(build|create|implement|generate|scaffold|develop)\b", re.IGNORECASE)
_BUILD_NOUN_RE = re.compile(
    r"\b(app|application|api|service|server|cli|tool|website|web ?app|webapp|"
    r"system|library|package|project|backend|frontend|module|engine|bot|"
    r"dashboard|parser|compiler|microservice)\b", re.IGNORECASE)
_BUILD_CUES = ("with test", "unit test", " files", "endpoints", "multiple file")


def is_advice_question(p: str) -> bool:
    """A question / advice request ("how do I …?", "should I …") — must never
    trigger an auto-escalation to the file-writing build pipeline."""
    p = (p or "").strip().lower()
    if p.endswith("?"):
        return True
    return bool(_ADVICE_RE.match(p))


def regex_build_fallback(p: str) -> bool:
    """Minimal 'looks like a fresh multi-file build' detector — the fallback
    when the LLM classifier gave no positive class. Excludes advice questions
    and tracker actions (create N jira tickets / a confluence page)."""
    p = (p or "").lower()
    if len(p) < 12 or is_advice_question(p):
        return False
    if _TRACKER_PLATFORM_RE.search(p) and _TRACKER_ITEM_RE.search(p):
        return False
    verb = _BUILD_VERB_RE.search(p)
    noun = _BUILD_NOUN_RE.search(p)
    cues = any(c in p for c in _BUILD_CUES)
    return bool(verb and (noun or cues))


@dataclass
class RouteDecision:
    doc_task: bool          # → research / analysis agent
    is_build_task: bool     # a fresh multi-file build
    build_escalate: bool    # simple-mode auto-escalation into the pipeline
    route_pipeline: bool    # run the PARALLEL decompose pipeline (else sequential)
    notice: str | None      # one router 'thought' to surface (or None)


def decide(prompt: str, *, agent_mode: str, team: bool, psub_on: bool,
           greenfield: bool, fresh: bool, cat: "str | None",
           team_approvals: bool, auto_escalate: bool = True) -> RouteDecision:
    """Compute the routing decision. Pure — all side-effecting inputs (the LLM
    ``cat``, ``fresh`` = not-a-follow-up, ``psub_on``, ``greenfield``,
    ``team_approvals``) are gathered by the caller and passed in.

    ``cat`` ∈ {chat,tracker,doc_analysis,code_build,code_edit} or None."""
    if cat is not None:
        doc_task = cat == "doc_analysis"
        is_build_task = cat == "code_build"
    else:
        doc_task = False                       # no positive doc class → single agent
        is_build_task = regex_build_fallback(prompt)
    # (C) PLAN owns its own analysis + yields a change-PLAN — never re-route a
    # plan turn to the research agent on a doc class.
    if agent_mode == "plan":
        doc_task = False
    # (A) an EXPLICIT team pick + a build-looking request is never downgraded to
    # the read-only research agent on a doc_analysis misclassification.
    if team and doc_task and regex_build_fallback(prompt):
        doc_task = False

    build_escalate = bool(
        not team and psub_on and agent_mode != "plan"
        and not is_advice_question(prompt)       # a question never auto-builds
        and not doc_task and auto_escalate and is_build_task)

    # (F) a FRESH explicit team request always pipelines (the greenfield/new-build
    # guard is only for simple-mode auto-escalation). (J) approvals ON forces the
    # SEQUENTIAL path (route_pipeline False → caller runs the gated pipeline).
    route_pipeline = bool(
        psub_on and not doc_task and not team_approvals
        and ((team and fresh)
             or ((team or build_escalate) and (greenfield or is_build_task))))

    notice = _notice(agent_mode=agent_mode, team=team, psub_on=psub_on,
                     doc_task=doc_task, is_build_task=is_build_task,
                     build_escalate=build_escalate, route_pipeline=route_pipeline,
                     team_approvals=team_approvals)
    return RouteDecision(doc_task, is_build_task, build_escalate,
                         route_pipeline, notice)


def _notice(*, agent_mode, team, psub_on, doc_task, is_build_task,
            build_escalate, route_pipeline, team_approvals) -> "str | None":
    """The single router 'thought' to surface for this decision (mutually
    exclusive branches → at most one). The doc-analysis dispatch emits its own
    notice at the fan-out site, so it's not handled here."""
    if build_escalate:
        return ("Multi-file build detected — routing through the build pipeline "
                "(decompose → scaffold → implement → test) instead of a single "
                "agent.")
    if not team and not psub_on and agent_mode != "plan" and is_build_task:
        return ("Multi-file build detected, but the parallel pipeline is disabled "
                "— running single-agent (sequential). Enable "
                "AIFORGE_PARALLEL_SUBTASKS=1 to decompose + fan out (set "
                "AIFORGE_PARALLEL_SUBTASKS_MAX=4 only if the model endpoint truly "
                "serves concurrent requests — a serial local endpoint gains nothing).")
    if team and not route_pipeline and not doc_task:
        if team_approvals:
            return ("Team + approvals ON → running the SEQUENTIAL pipeline so "
                    "every risky tool pauses for your Approve/Reject (the "
                    "parallel path can't gate). Turn Pipeline approvals off for "
                    "the faster parallel build.")
        return ("Existing code + a targeted change — sequential in-place pipeline "
                "(history + current files in context), not a from-scratch "
                "parallel rebuild.")
    return None


__all__ = ["is_advice_question", "regex_build_fallback", "RouteDecision", "decide"]
