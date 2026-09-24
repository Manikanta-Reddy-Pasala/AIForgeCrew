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
# A named file ("hello.py", "a.txt", "build.gradle"): name, dot, an extension
# starting with a letter (so "3.11" or "v1.2" is not a file), plus the
# extensionless build files.
_NAMED_FILE_RE = re.compile(
    r"\b[\w\-/]+\.[a-z][a-z0-9]{0,9}\b"
    r"|\b(?:docker|make|proc|jenkins|gem|rake|vagrant)file\b", re.IGNORECASE)
# Dotted names that are frameworks or abbreviations, not files to create.
_NOT_FILES = frozenset((
    "e.g", "i.e", "node.js", "next.js", "nuxt.js", "vue.js", "react.js",
    "express.js", "three.js", "d3.js", "chart.js", "nest.js", "ember.js"))
# A kind of program that means real code to design, not a snippet.
_SMALL_BLOCK_NOUN_RE = re.compile(
    r"\b(scraper|crawler|game|site|website|pages|plugin|extension|component|"
    r"pipeline|converter|manager|solver)s?\b", re.IGNORECASE)
# "five python files", "12 files": four or more files is a multi-file build.
_MANY_FILES_RE = re.compile(
    r"\b(\d+|four|five|six|seven|eight|nine|ten)\s+(?:\w+\s+){0,2}files\b",
    re.IGNORECASE)
# Anything that says "this is more than a snippet": tests, several modules,
# persistence, an explicit multi-file layout.
_MULTI_PART_RE = re.compile(
    r"\btests?\b|endpoint|multiple files?|several files?|storage|database|"
    r"structure", re.IGNORECASE)
_SMALL_MAX_FILES = 3
_SMALL_MAX_CHARS = 200


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


_CHORE_RE = re.compile(
    r"\b(run|runs|execute|print|prints|echo|empty|touch|ls|cat|list the files)\b")


def is_small_task(p: str) -> bool:
    """A trivial file/shell chore ("create a.txt b.txt c.txt then run ls",
    "create hello.py and run it") that a single agent finishes in a few calls.

    The classifier (and the " files" regex cue) call these BUILD because they
    create files, and escalation then spent 40+ model calls and minutes on the
    enhance → architect → plan → parallel team pipeline, which even failed to
    build three empty files. Deliberately narrow: it only fires when the ask
    is short, names one to three concrete files, is a chore (run / print /
    empty / touch / ls), and carries no app/service/game/scraper noun and no
    tests / storage / many-files cue, so real feature work still escalates."""
    p = (p or "").lower()
    if len(p) >= _SMALL_MAX_CHARS:
        return False
    named = {f for f in _NAMED_FILE_RE.findall(p)
             if f not in _NOT_FILES and not f.endswith((".io", ".net"))}
    if not named or len(named) > _SMALL_MAX_FILES:
        return False
    # Judge the words, not the file names: "app.py" or "cli.py" is one file,
    # while "an app" or "a cli" is a build.
    rest = _NAMED_FILE_RE.sub(" ", p)
    if (_BUILD_NOUN_RE.search(rest) or _SMALL_BLOCK_NOUN_RE.search(rest)
            or _MULTI_PART_RE.search(rest)):
        return False
    m = _MANY_FILES_RE.search(rest)
    if m and (not m.group(1).isdigit() or int(m.group(1)) > 3):
        return False
    # Only a CHORE: running it, printing, empty/touched files, listing. Feature
    # work that names a file or two ("implement OAuth in auth.py and routes.py")
    # keeps the build pipeline's plan and review.
    return bool(_CHORE_RE.search(rest))


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
    # Simple mode only: a trivial file/shell chore stays on the single agent
    # even when it reads like a build. An explicit team pick is left alone.
    if (is_build_task and not team and agent_mode != "plan"
            and is_small_task(prompt)):
        is_build_task = False
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


__all__ = ["is_advice_question", "regex_build_fallback", "is_small_task", "RouteDecision", "decide"]
