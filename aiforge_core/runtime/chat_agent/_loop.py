from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import types
from collections.abc import Callable, Iterator
from pathlib import Path

from ._shell import (_MAX_OBS, _MAX_OBS_READ, _READ_OBS_TOOLS, _smart_truncate_obs)
from ._tools import (_ROOT_SCOPED_TOOLS, _chat_repo_key, _preferences_context, _rules_context, _scoped_root)
from ._registry import (TOOLS, _ANALYZE_BANNER, _BUILDER_FINALIZE_TOOL, _BUILDER_NUDGE_AFTER, _FINALIZE_TOOLS, _PLAN_BANNER, _READONLY_TOOLS, _is_mutating, _perf_family)
from ._preview import (_diff_preview)
from ._prompt import (_SYSTEM, _parse, _strip_reasoning_prefix)
from ._context import (_CANCELLED, _EDIT_TOOL_NAMES, _LOOP_REPEAT, _OUTPUT_REPEAT, _WEB_LOOKUP_DIRECTIVE, _cap_system_prompt, _cave_mode, _chat_session_recall, _claims_file_edits, _compact_convo, _complete_cancellable, _compress_prompt, _ctx_budget_chars, _ctx_on, _edit_claim_disclaimer, _edit_claim_guard_enabled, _edit_claim_nudge, _fire_stop, _has_web_intent, _post_edit_syntax_error, _progress_recap, _extension_budget, _repo_name, _stuck_recovery_max, _run_project_verify, _safety_cap, _split_asks, _sys_prompt_budget_chars, _unattended_cap, _text_of, _turn_deadline_s, _verify_fix_message, _verify_max_rounds, _verify_on_final_enabled, _worktree_fingerprint)

_log = logging.getLogger("aiforge.chat_agent")

_THE_FINALIZE_TOOL = 'the finalize tool'

# Cap on the per-turn signature tables (the action-strike table and the
# "files this turn has ever read" set). Both were bounded in practice by the
# 2000-step safety cap; an uncapped turn removes that ceiling.
_ACTION_SIG_MAX = 5000


def _max_gen_per_step() -> int:
    """Total model generations one ReAct step may cost, across every retry
    layer (transport retry, empty-answer re-post, this loop's own sweep).

    Retrying a call that is failing for a structural reason does not produce a
    better answer, it produces the same non-answer again at full price — and
    the layers multiply: 5 transport attempts x 4 empty re-posts x 8 sweeps is
    a hundred and sixty generations for one step. This is the ceiling on that
    product. Raised to 10 (from 6) so a genuinely unavailable model — one that
    is reloading or briefly unreachable — gets the fuller retry budget the
    sweeps below now ask for, while still bounding a structurally-failing call;
    0 disables the ceiling and restores the old per-layer behaviour.
    """
    try:
        _v = int(os.environ.get("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "10"))
    except ValueError:
        return 10
    # A NEGATIVE value reads as "tighter than zero" and used to clamp to 0,
    # which means DISABLED — the opposite of what the operator typed. Only an
    # explicit 0 turns the ceiling off; anything else nonsensical falls back to
    # the default rather than silently removing the bound.
    return _v if _v >= 0 else 10


def _codegraph_directive(cwd, readonly_mode) -> str:
    """Ensure this repo's codegraph index (skipped in read-only modes — the
    build writes a .codegraph/ dir into the repo) and return the "CODEGRAPH IS
    AVAILABLE" tool directive when the shared gate says it is usable this run,
    else ''."""
    try:
        from aiforge_core.runtime.tools import codegraph as _cg
        if not readonly_mode:
            _cg.ensure_indexed(cwd)
        if _cg.enabled_for_run(cwd):
                return (
                    "\n\nCODEGRAPH IS AVAILABLE (a pre-built code-relation index "
                    "for THIS repo). USE IT — do not rediscover with grep what "
                    "the graph already knows:\n"
                    "- BEFORE editing or extending any EXISTING symbol, call "
                    "codegraph_callers AND codegraph_impact on it to find every "
                    "call site + everything a change would affect. Grep misses "
                    "call sites and matches comments/strings; the graph does not.\n"
                    "- To ORIENT on an unfamiliar area, call codegraph_explore "
                    "with the task in plain words FIRST (before list_dir/grep).\n"
                    "- To locate a definition, use codegraph_query, not grep.\n"
                    "Tools:\n"
                    "- codegraph_explore  {\"query\": \"where amounts are parsed\"}  "
                    "relevant symbols + their source for a natural-language query\n"
                    "- codegraph_query    {\"query\": \"clean_amount\"}   find a "
                    "symbol + its defining file:line\n"
                    "- codegraph_callers  {\"symbol\": \"foo\"}   every caller of foo\n"
                    "- codegraph_callees  {\"symbol\": \"foo\"}   what foo calls\n"
                    "- codegraph_impact   {\"symbol\": \"foo\"}   blast-radius of "
                    "changing foo — ALWAYS call before editing a shared symbol")
    except Exception:  # noqa: BLE001 — never break prompt build
        pass
    return ""


def _append_session_blocks(add, cwd, messages, session_id, role):
    """Append per-session context: attached-image descriptions (and return this
    turn's vision image parts), the execution ledger, and the optional OKR-DAG
    goal context. Returns ``img_blocks``."""
    # SESSION IMAGES: descriptions of images the user attached, so the (maybe
    # text-only) model can answer questions about them all session long.
    _img_blocks: list[dict] = []
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_media
            add("images", chat_media.context_block(session_id))
            _img_blocks = chat_media.image_blocks_for_turn(session_id, role)
        except Exception:  # noqa: BLE001 — images must never break a turn
            _img_blocks = []
        # EXECUTION LEDGER: what this session ALREADY ran (exact commands + files
        # + outcomes) so a follow-up doesn't redo completed work.
        try:
            from aiforge_core.runtime import session_ledger
            add("executed", session_ledger.ledger_block(session_id))
        except Exception:  # noqa: BLE001 — ledger must never break a turn
            pass
    # OKR-DAG: surgical goal context for the ACTIVE Key Result — the separate
    # Objective→KR→Learning→Session node graph under memory/okr/. CONSOLIDATED
    # OUT by default (AIFORGE_OKR_DAG=1 to re-enable): the flat compacted-<scope>
    # briefs (project-memory block above) are the single OKR knowledge memory now;
    # the DAG duplicated them with a staler parallel structure.
    if os.environ.get("AIFORGE_OKR_DAG", "0") == "1":
        try:
            from aiforge_core.memory import okf as _okr
            # repo-scoped AND query-relevant: the global rules + THIS repo's
            # learnings/solutions most related to the CURRENT ask.
            _q = next((m.get("content") or "" for m in reversed(messages)
                       if m.get("role") == "user"), "")
            add("okr", _okr.context_block(
                repo=_chat_repo_key(cwd), query=_q))
        except Exception:  # noqa: BLE001 — okr context must never break a turn
            pass
    return _img_blocks


def _append_learning_recall(add, bundle, last_user, session_id, proactive,
                            is_init, prev_session_on, cwd=None):
    """Append self-learning recall: in FULL mode dump memory recall + prior-chat
    hits (excluding the prev-session already injected); in LITE dump recall only
    on the opening turn and otherwise point the model at the memory tools."""
    _proactive = proactive
    _is_init = is_init
    _prev_session_on = prev_session_on
    _bundle = bundle
    if _ctx_on("recall") and _proactive == "full":
        add("recall", _bundle.memory_md)
        # Prior CHAT SESSIONS — surface what the user discussed in OTHER
        # conversations (excludes the current session). Cave mode → fewer hits.
        # Local SQLite scan, so cheap enough to run every turn there IS a query.
        if last_user:
            # Keep prior-chat recall, but when the prev-session continuity block
            # is injected, exclude ONLY that one session's hits (not all of
            # them) so older relevant sessions still surface (audit R6).
            _drop = None
            if _prev_session_on:
                try:
                    from aiforge_core.runtime import chat_okr as _cokr
                    # Same cwd filter the brief used — otherwise this drops a
                    # DIFFERENT session than the one that was injected.
                    _drop = _cokr.previous_session_id(session_id, cwd=cwd)
                except Exception:  # noqa: BLE001
                    _drop = None
            add("chat-recall", _chat_session_recall(
                last_user, session_id, limit=4,
                drop_session=_drop))
    elif _ctx_on("recall"):
        # LITE (default): don't pre-dump on follow-ups — but the SESSION-START
        # turn still gets the one-time recall keyed to the opening request.
        if _is_init:
            add("recall", _bundle.memory_md)
        # Tell the model it HAS memory + the tools to reach it, so it pulls
        # only what THIS turn needs.
        add("memory-tools",
            "MEMORY: a project brief for this repo is above. For anything "
            "specific you don't already see — past decisions/learnings, code, "
            "symbols, or what was discussed in earlier chats — CALL the tools: "
            "memory_lookup(query) for learnings/decisions, graphify_lookup for "
            "concept-graph, grep/repo_map/read for code, search_chat_sessions "
            "for prior chats. Look it up; don't guess or assume it's absent.")

def _append_recall_blocks(add, bundle, cwd, last_user, messages, session_id,
                          role, proactive, is_init):
    """Append the self-learning recall blocks (memory recall + prior chat
    sessions in full mode, or a memory-tools pointer in lite), previous-session
    continuity, session images, the execution ledger, and the optional OKR-DAG.
    Returns this turn's vision ``img_blocks``."""
    _is_init = is_init
    _proactive = proactive
    _bundle = bundle
    # Whether the explicit PREVIOUS-SESSION continuity block will be injected
    # (opening turn). When it is, the separate chat-session recall below is
    # redundant — the recall bundle already carries a prior-chat source and the
    # prev-session block carries the immediate prior conversation — so skip it to
    # avoid surfacing the same session twice (audit R6).
    # PREVIOUS SESSION continuity — at session START, carry the last SAME-PROJECT
    # conversation forward so a follow-up asked in a NEW chat has its context
    # (the tail of that session, framed REFERENCE-ONLY — no resuming its task, and
    # a contradicting new ask wins). Built FIRST, because whether it is non-empty
    # is what decides the recall exclusion below; a different project's session no
    # longer qualifies, so the brief is often "" and that session's recall hits
    # must then NOT be dropped. Cheap local scan, opening turn only;
    # AIFORGE_SESSION_PREV_CONTEXT=0 disables. Kept in cave too — it's quality
    # continuity, not growing history; the cap trims it only if the window is
    # genuinely tight.
    _prev_brief = ""
    if (_is_init and session_id is not None
            and os.environ.get("AIFORGE_SESSION_PREV_CONTEXT", "1") != "0"):
        try:
            from aiforge_core.runtime import chat_okr as _cokr
            _prev_brief = _cokr.previous_session_brief(session_id, cwd=cwd) or ""
        except Exception:  # noqa: BLE001 — continuity must never break a turn
            _prev_brief = ""
    # When the block IS injected, the separate chat-session recall below is
    # redundant for that one session — the recall bundle already carries a
    # prior-chat source and the prev-session block carries the conversation — so
    # skip it there to avoid surfacing the same session twice (audit R6).
    _prev_session_on = bool(_prev_brief)
    # Self-learning recall — EVERY turn, keyed to the CURRENT user message
    # (from the shared bundle). Cave mode pulls fewer hits.
    _append_learning_recall(add, _bundle, last_user, session_id,
                            _proactive, _is_init, _prev_session_on, cwd)
    if _prev_brief:
        add("prev-session", _prev_brief)
    _img_blocks = _append_session_blocks(add, cwd, messages, session_id, role)
    return _img_blocks


def _append_context_blocks(add, cwd, last_user, messages, session_id, role, cave):
    """Append every dynamic context block (project memory, seed TOC, repo
    summary, workflows, skills, repo-map, @-mentions, self-learning recall +
    prior chats, prev-session continuity, session images, execution ledger, and
    the optional OKR-DAG) to the system prompt via ``add(label, block)``.
    Returns ``(bundle, img_blocks)`` (the vision image parts for this turn)."""
    # Dynamic context blocks — via the SHARED bundle builder (same source
    # selection/scoping/gating as chat-team + the pipeline). rules+prefs are
    # already injected above as high-priority blocks, so skip them here.
    from aiforge_core.runtime import context_bundle as _cb
    # Proactive-recall mode. "lite" (default): send a SMALL anchor (repo summary
    # + the compacted project brief) and let the model PULL specifics via the
    # memory tools on demand — instead of pre-dumping the full recall every turn.
    # "full": the old behaviour (dump memory_md + prior-session recall upfront).
    # EXCEPTION even in lite: the SESSION-START turn injects one recall keyed to
    # the opening request, so the agent arrives informed (self-learning) instead
    # of re-deriving what past sessions worked out.
    _proactive = os.environ.get(
        "AIFORGE_CHAT_PROACTIVE_RECALL", "lite").strip().lower()
    _is_init = not any(m.get("role") == "assistant" for m in messages)
    # In lite mode a FOLLOW-UP turn doesn't inject recall at all — skip the
    # unified_query work too instead of building a block that gets dropped.
    _recall_wanted = _proactive == "full" or _is_init
    _bundle = _cb.build_bundle(
        cwd, last_user, cave=cave,
        ctx_on=lambda b: _ctx_on(b) and (b != "recall" or _recall_wanted),
        session_id=session_id, want_rules=False, want_prefs=False)
    # Project memory (compacted per-repo brief) — small + high-value; the
    # "you already know this repo" anchor. Always injected.
    add("project-memory", _bundle.project_brief_md)
    # Seed memory / concept index — a compact TOC of EVERY brief so the agent
    # knows what memory exists to recall (the "amnesia" fix: a model never queries
    # memory it doesn't know is there). Gated by AIFORGE_SEED_TOC; embedded only.
    try:
        from aiforge_core.memory import backend_select as _bsel2
        if _bsel2.embedded():
            from aiforge_core.memory import md_store as _mds2
            add("memory-index", _mds2.seed_memory_block())
    except Exception:  # noqa: BLE001 — seed TOC must never break a turn
        pass
    if _ctx_on("summary"):
        add("repo-summary", _bundle.repo_summary_md)
    # WORKFLOWS before the (big) repo-map, and NOT skipped in cave mode: a
    # matched workflow is a MANDATORY user procedure (branch/MR conventions,
    # naming) — dropping it silently made the agent e.g. commit straight to
    # main. Append order = drop order under a tight window, so procedures
    # must outrank the repo-map (the agent can always grep structure back).
    if _ctx_on("workflows"):
        add("workflows", _bundle.workflows_md)
    # SKILLS are static QUALITY context (how to do the task right), not the
    # growing history that makes small models drift — so cave KEEPS them. Token
    # safety comes from condensing HISTORY early + the _cap_system_prompt
    # backstop (which drops the lowest-priority TAIL first, and skills are
    # ordered ABOVE the repo-map so they survive a tight window). Dropping
    # skills to save tokens was a quality regression; don't.
    if _ctx_on("skills"):
        add("skills", _bundle.skills_md)
    if _ctx_on("repomap"):
        add("repo-map", _bundle.repo_map_md)
    # @-mentions — static quality context too; KEEP in cave (the cap trims it
    # from the tail only if the window is genuinely too tight).
    if _ctx_on("mentions"):
        try:
            from aiforge_core.runtime import mentions as _mentions
            ment_block, _toks = _mentions.expand(last_user, cwd)
            add("mentions", ment_block)
        except Exception:  # noqa: BLE001
            pass
    _img_blocks = _append_recall_blocks(
        add, _bundle, cwd, last_user, messages, session_id, role,
        _proactive, _is_init)
    return _bundle, _img_blocks


def _prepend_priority_blocks(sys_msg, asks, prefs, rules, analyze_mode,
                             plan_mode, builder):
    """Prepend the highest-priority prompt blocks in drop order (multi-ask
    checklist, standing prefs, user rule book, analyze/plan banner, builder
    charter) — each pushed to the FRONT so it survives a tight window. Returns
    the augmented ``sys_msg``."""
    _asks = asks
    if _asks:
        sys_msg = ("MULTI-PART REQUEST — the user's CURRENT message contains "
                   f"{len(_asks)} distinct asks. Address EVERY one; number "
                   "your final answer to match. Checklist:\n"
                   + "\n".join(f"{i + 1}. {a}" for i, a in enumerate(_asks))
                   + "\nTRACK your progress: when you START part N call "
                     'ACTION: plan_progress ARGS_JSON: {"slug": "part-N", '
                     '"status": "running"}, and when it is DONE call it again '
                     'with "status": "done" — the user watches this live.'
                   + "\n\n" + sys_msg)
    if prefs:                       # standing user preferences — always applied
        sys_msg = prefs + "\n\n" + sys_msg
    if rules:                       # user rule book first — highest priority
        sys_msg = rules + "\n\n" + sys_msg
    if analyze_mode:                # read-only ANALYSIS (findings, not a plan)
        sys_msg = _ANALYZE_BANNER + "\n\n" + sys_msg
    elif plan_mode:                 # plan banner second — constrains this turn
        sys_msg = _PLAN_BANNER + "\n\n" + sys_msg
    if builder:                     # task-specific builder charter (highest)
        try:
            from aiforge_core.runtime.prompts_extended import builders as _bld
            _charter = _bld.charter_for(builder)
        except Exception:  # noqa: BLE001 — a bad charter must never break chat
            _charter = None
        if _charter:
            sys_msg = _charter + "\n\n" + sys_msg
    return sys_msg


def _seed_prompt(messages, cwd, readonly_mode):
    """Seed the system prompt: extract the raw last-user message, load cave/rules/
    prefs, format the core prompt, and apply the catalog + codegraph gates.
    Returns (last_user, cave, rules, prefs, sys_msg)."""
    last_user = next(
        (_text_of(m) for m in reversed(messages)
         if (m.get("role") or "user") == "user" and m.get("content")), "")
    # _text_of flattens a multimodal (vision) turn's list content to text, so the
    # .split() below can't crash on a list.
    last_user = last_user.split("\n\n---\n[Interpreted request")[0].strip() or last_user

    # Inject a fresh repo map every turn so the agent ALWAYS knows the
    # directory structure of the working dir without re-searching it on
    # each follow-up question (the conversation history only carries prior
    # answers, not the structure it discovered last turn).
    cave = _cave_mode()
    rules = _rules_context(cwd, last_user)
    prefs = _preferences_context(cwd)
    sys_msg = _SYSTEM.format(cwd=cwd)
    # Advertise only integrations this install can reach. Same principle as the
    # CodeGraph gate below: a tool the model is told about but that always
    # answers `*_not_configured` costs prompt budget and invites a wrong pick.
    try:
        from ._catalog_gate import gate_catalog
        sys_msg, _ungated = gate_catalog(sys_msg)
    except Exception:  # noqa: BLE001 — never let gating break a turn
        pass
    # CodeGraph tools are advertised ONLY when actually usable on this run — the
    # single shared gate (binary + real index for THIS repo + not env-disabled +
    # not opted out per-ticket). Otherwise the model would be told to call a tool
    # that always errors (un-indexed repo) / the A/B "without" arm would leak.
    # Without this block the tools are in TOOLS but absent from the catalog, so
    # the model never learns they exist.
    sys_msg += _codegraph_directive(cwd, readonly_mode)
    return last_user, cave, rules, prefs, sys_msg


def _build_convo(messages, cwd, role, *, readonly_mode, plan_mode,
                 analyze_mode, builder, strict_finish, session_id):
    """Build the ReAct conversation: assemble the budget-capped system prompt
    (rules, prefs, banners, catalog/codegraph gates, multi-ask checklist, and
    every dynamic context block via the shared bundle), fold history + vision
    images into the message list. Returns
    ``(convo, bundle, asks, dropped_playbooks)``."""
    last_user, cave, rules, prefs, sys_msg = _seed_prompt(messages, cwd, readonly_mode)
    # Multi-part message (simple mode has no enhancer/spec, so nothing else
    # tracks the parts): derive an ASK CHECKLIST and pin it HIGH in the
    # system prompt — the model must cover every part, not answer #1 and stop.
    # Skipped on strict_finish (pipeline Doer / subtask runners): there the
    # "user message" is a MACHINE-built seed whose instruction bullets
    # ("CONTEXT-FIRST…", "MINIMAL DIFF…") are style rules, not asks — counting
    # them made the Doer enumerate its own charter in FINAL and burned an extra
    # model turn on the completeness gate every run.
    _asks = [] if (builder or strict_finish) else _split_asks(last_user)
    sys_msg = _prepend_priority_blocks(
        sys_msg, _asks, prefs, rules, analyze_mode, plan_mode, builder)
    # C2: budget the (un-condensable) system prompt. The CORE prompt + rules
    # above are ALWAYS kept; each optional block below is appended via a
    # budget-aware helper that truncates/drops it (lowest priority = appended
    # last = dropped first) when it would blow the cap. `_cap_system_prompt`
    # is the final backstop guaranteeing len(sys_msg) <= cap.
    _sys_cap = _sys_prompt_budget_chars(role)
    _sys_core_len = len(sys_msg)
    _sys_dropped: list[str] = []
    _sys_seen_blocks: set[str] = set()

    def _add_sys_block(label: str, block: str) -> None:
        nonlocal sys_msg
        if not block:
            return
        # R7: don't spend budget on a block whose exact text was already added
        # (e.g. prev-session vs a recall block that surfaced the same content).
        _bkey = " ".join(block.split())
        if _bkey in _sys_seen_blocks:
            return
        _sys_seen_blocks.add(_bkey)
        addition = "\n\n" + block
        if len(sys_msg) + len(addition) <= _sys_cap:
            sys_msg += addition
            return
        room = _sys_cap - len(sys_msg)
        if room > 400:              # enough left for a meaningful truncated slice
            sys_msg += addition[:room] + "\n…(truncated to fit context)\n"
        _sys_dropped.append(label)

    # WEB-LOOKUP directive FIRST — it's short + critical, so it must outrank the
    # big optional blocks (repo-map/recall) under a tight window (blocks added
    # LATER drop first). Without top priority the "you MUST web_search" nudge got
    # trimmed exactly when context was full, and the model answered from stale
    # memory. Read-only tool → safe in plan/analyze too. (Detected on last_user;
    # a bare URL is excluded — it already routes to web_crawl.)
    if last_user and _has_web_intent(last_user):
        _add_sys_block("web-lookup", _WEB_LOOKUP_DIRECTIVE)

    _bundle, _img_blocks = _append_context_blocks(
        _add_sys_block, cwd, last_user, messages, session_id, role, cave)
    if _sys_dropped:                # one-line note so the trim is visible
        _add_sys_block("_note", "[context note: dropped/trimmed lower-priority "
                       "blocks to fit the window: " + ", ".join(_sys_dropped) + "]")
    # A dropped WORKFLOWS/SKILLS block means the agent may skip a mandatory
    # user procedure (e.g. branch-then-MR) — surface that to the USER instead
    # of failing silently inside the prompt.
    _dropped_playbooks = [b for b in ("workflows", "skills") if b in _sys_dropped]
    # Final backstop: guarantee the system prompt is under the cap (keeps the
    # core + rules at the front; truncates the injected tail).
    sys_msg = _cap_system_prompt(sys_msg, _sys_cap, protect=_sys_core_len)
    sys_msg = _compress_prompt(sys_msg)   # trim whitespace bloat (caveman-style)
    convo = _history_to_convo(sys_msg, messages, _img_blocks)
    return convo, _bundle, _asks, _dropped_playbooks


def _history_to_convo(sys_msg, messages, _img_blocks):
    """Assemble the message list: system prompt + each history turn, then fold
    this turn's vision image parts into the latest user turn (multimodal
    content) when the model is vision-capable. Returns ``convo``."""
    convo: list[dict] = [{"role": "system", "content": sys_msg}]
    for m in messages:
        r = m.get("role") or "user"
        convo.append({"role": "assistant" if r == "assistant" else "user",
                      "content": m.get("content") or ""})
    # When the model is vision-capable, fold the actual images into the latest
    # user turn (multimodal content) so it can SEE them, not just their text.
    if _img_blocks:
        for _m in reversed(convo):
            if _m.get("role") == "user":
                _m["content"] = [{"type": "text", "text": _m.get("content") or ""},
                                 *_img_blocks]
                break
    return convo


_RETRY_STOP = object()


def _retry_plan(exc, _step_calls):
    """Compute the completion-retry plan: the retry count (env default, capped
    by the per-step generation budget, forced to 0 for a shipped read-timeout or
    an unserved-model config error) plus the per-step budget and a config-error
    message. Returns ``(retries, budget, cfg_error)``."""
    _cfg_error = ""
    _retries = 8
    try:
        _retries = max(0, int(os.environ.get("AIFORGE_CHAT_LLM_RETRIES", "8")))
    except ValueError:
        _retries = 8
    # BOUND THE PRODUCT, not this layer alone. Below this sweep the
    # client already re-posts an empty answer (AIFORGE_LLM_EMPTY_RETRIES,
    # default 3 → 4 posts) and the transport retries a broken one
    # (AIFORGE_LLM_RETRY_MAX, default 3), so five more sweeps here is up
    # to twenty full generations for ONE step — every one of them
    # shipping the whole prompt and generating an answer nobody reads.
    # The meter already counts what this turn has actually sent, so
    # spend the remaining budget instead of a fixed count.
    _spent = int((_step_calls or {}).get("n") or 0)
    _budget = _max_gen_per_step()
    if _budget > 0:      # 0 = ceiling disabled, not "no retries"
        _retries = min(_retries, max(0, _budget - _spent))


    # A read timeout means the model RECEIVED this prompt and is
    # still generating it. Re-issuing the identical completion leaves
    # that generation running and starts another on a box that already
    # could not finish one — five more times, by default. The transport
    # marks the exception; honour it here, or the layer below's
    # "do not re-POST" rule is undone one call up.
    try:
        from aiforge_core.llm.client import shipped_timeout as _st
        if _st(exc):
            _retries = 0
    except Exception:  # noqa: BLE001
        pass
    # A model the endpoint does not serve is CONFIGURATION. Retrying it
    # cannot work — five more full-prompt round trips, each answered
    # with the same 400, then the same useless "didn't respond" line.
    # Say what is wrong instead; the exception already names the model,
    # the endpoint and what that box does serve.
    _cfg_error = ""
    try:
        from aiforge_core.llm.client import model_missing as _mm
        if _mm(exc):
            _retries = 0
            _cfg_error = str(exc).split(" — ", 1)[-1].strip()
    except Exception:  # noqa: BLE001
        pass
    return _retries, _budget, _cfg_error


def _emit_completion_failure(_cfg_error, _meter, _step_tok):
    """Emit the user-facing completion-failure message + the structural
    ``stopped``/``done`` markers (so chat_resume knows the turn died mid-work),
    and release the step meter."""
    yield {"type": "message", "text": (
        f"⚠️ {_cfg_error}" if _cfg_error else
        "⚠️ The model didn't respond (it may be loading, busy, or the "
        "request was rejected). Nothing was changed — please try again "
        "in a moment. If it keeps happening, check the model endpoint.")}
    # STRUCTURAL marker, the same one a Stop leaves: this turn ended
    # without an answer, and whatever it had already read or written
    # is on disk. Without it `chat_resume` sees a turn that "ended
    # normally" with a warning as its answer, so Retry starts from
    # nothing and re-does every edit the dead attempt made — the
    # exact case a resume exists for, and the one it was missing.
    yield {"type": "stopped", "reason": "llm_unavailable"}
    yield {"type": "done"}
    if _meter is not None:
        _meter.step_reset(_step_tok)

def _retry_completion(complete_fn, role, convo, session_id, exc,
                      _step_calls, _meter, _step_tok):
    """Recover a failed model completion: retry (bounded by the per-step
    generation budget; 0 retries for a shipped-timeout or unserved-model error)
    with escalating backoff. Yields progress/stop events; returns the completion
    text (possibly None) on recovery, or ``_RETRY_STOP`` when the caller must end
    the turn."""
    from aiforge_core.runtime import chat_cancel
    # RESILIENCE: a local model can transiently drop a request (mid-load,
    # busy, a one-off empty/4xx). Retry a few times before surfacing, and
    # never show the raw `llm.exhausted role=chat …` stack; give a plain,
    # actionable message.
    # AIFORGE_CHAT_LLM_RETRIES tunes the retry count (default 8) — a
    # local model that's loading/busy often needs a few passes.
    _retries, _budget, _cfg_error = _retry_plan(exc, _step_calls)
    def _over_budget() -> bool:
        """Has this STEP spent its generation budget yet?

        Re-read every sweep, because one sweep is not one generation:
        below this loop the client re-posts an empty answer and the
        transport re-attempts a broken one, so a single sweep can burn
        four or twelve. Extrapolating the whole step from the first
        sample let a declared ceiling of 6 spend 12 — the very
        multiplication this exists to stop."""
        if _budget <= 0 or _step_calls is None:
            return False
        return int(_step_calls.get("n") or 0) >= _budget
    out = None
    _last = exc
    for _rn in range(_retries):
        if session_id is not None and chat_cancel.is_cancelled(session_id):
            break
        if _over_budget():
            break
        yield {"type": "thought", "role": "system",
               "text": f"⟳ model didn't respond — retrying ({_rn + 1}/{_retries})…"}
        # Escalating backoff: give a mid-load / busy local model (or a
        # slow compress+forward hop) progressively more room to recover.
        time.sleep(3.0 * (_rn + 1))
        try:
            out = _complete_cancellable(complete_fn, role, convo, session_id)
            _last = None
            break
        except Exception as exc2:  # noqa: BLE001
            _last = exc2
    if _last is not None:
        yield from _emit_completion_failure(_cfg_error, _meter, _step_tok)
        return _RETRY_STOP
    return out


def _invoke_tool(fn, name, args, cwd):
    """Run one tool fn under a scoped sandbox-root override (reset in finally)
    with perf recording; KeyError/Exception become an error result."""
    _perf_t0 = time.perf_counter()
    # Strong tools resolve through sandbox.root(); scope the override to
    # the workspace root (NOT the raw cwd, so it can't escape an
    # AIFORGE_WORKSPACE_DIR jail) and ALWAYS reset it in finally so a
    # reused thread can't leak this session's dir into the next.
    _root_tok = None
    if name in _ROOT_SCOPED_TOOLS:
        try:
            from aiforge_core.runtime import sandbox as _sb
            _root_tok = _sb.set_root_override(_scoped_root(cwd))
        except Exception:  # noqa: BLE001
            _root_tok = None
    try:
        result = fn(args, cwd)
    except KeyError as exc:
        result = {"ok": False, "error": f"missing arg: {exc}"}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}
    finally:
        if _root_tok is not None:
            try:
                from aiforge_core.runtime import sandbox as _sb
                _sb.reset_root_override(_root_tok)
            except Exception:  # noqa: BLE001
                pass
    try:
        from aiforge_core.runtime import perf_recorder
        perf_recorder.record(
            _perf_family(name), name,
            (time.perf_counter() - _perf_t0) * 1000.0)
    except Exception:  # noqa: BLE001 — perf must never break a run
        pass
    return result


def _dispatch_tool(name, args, cwd, n, _hook_block):
    """Dispatch one tool call: honour a PreToolUse hook block / unknown tool,
    else emit ``tool_start`` and run ``fn(args, cwd)`` under a scoped sandbox
    root override (reset in finally) with perf recording. Returns the result
    dict."""
    fn = TOOLS.get(name)
    if _hook_block is not None:
        result = {"ok": False, "blocked": "hook", "hook": _hook_block,
                  "error": f"'{name}' was blocked by a PreToolUse hook"}
    elif fn is None:
        result = {"ok": False, "error": f"unknown tool: {name}"}
    else:
        # Live "it's running" signal — a slow tool (bash/test/build) used
        # to show NOTHING until `fn` returned, so the UI looked stalled
        # for however long the command actually took. `call_id` (the
        # ReAct step counter `n`, unique per iteration) lets the UI match
        # this to the completed `tool` event below and flip it in place
        # instead of appending a second, duplicate row.
        yield {"type": "tool_start", "name": name, "args": args,
               "call_id": n}
        result = _invoke_tool(fn, name, args, cwd)
    return result


def _command_gate_flags(name, args, cwd, session_id, _mode_approvals):
    """For a shell-family tool, detect a destructive delete (unless approvals
    are off for this interactive chat mode, or the allow_delete opt-in
    auto-confirms it) and whether a scoped commit-auto-approve flag applies to a
    whole-command git commit/add. May set args[confirm_delete].
    Returns ``(destructive_del, auto_commit)``."""
    _destructive_del = False
    _auto_commit = False
    if name in ("run_command", "bash", "run_shell", "shell", "serve",
                "watch_until"):
        _cmd = args.get("cmd") or args.get("command") or ""
        try:
            from aiforge_core.runtime.tools import delete_guard
            _destructive_del = (not delete_guard.allow_delete(
                ("AIFORGE_CHAT_ALLOW_DELETE", "AIFORGE_ALLOW_DELETE"))
                and delete_guard.is_destructive_delete(_cmd))
        except Exception:  # noqa: BLE001
            _destructive_del = False
        try:
            from aiforge_core.runtime import rule_capture as _rc
            _repo = _repo_name(cwd)
            # commit_auto_approve is consulted REGARDLESS of the per-mode
            # approval toggle. Gating it on ``not _mode_approvals`` made it
            # dead code: with approvals off nothing gates anyway, so the
            # flag — and the UI pill that sets it — could never have an
            # effect. It is an explicit, scoped, revocable, audited opt-in
            # for exactly one thing (a whole-command local git commit/add),
            # and every floor below still gates: DENY, destructive delete,
            # forced review, and any chained command (is_commit_command
            # rejects those). PUSH is excluded here as it is in tool_gate —
            # it updates a remote, so it always asks.
            if _rc.is_commit_command(_cmd) \
                    and not re.search(r"\bgit\s+push\b", _cmd, re.I) \
                    and _rc.flag_active("commit_auto_approve", repo=_repo,
                                        session_id=session_id):
                _auto_commit = True
            # allow_delete stays SUBORDINATE to the toggle: auto-confirming
            # an `rm -rf` in a mode whose approvals are on is a far bigger
            # relaxation than skipping a commit prompt, and nothing asked
            # for it. Off, it is the captured per-repo/session opt-in.
            if not _mode_approvals and _destructive_del and _rc.flag_active(
                    "allow_delete", repo=_repo, session_id=session_id):
                _destructive_del = False
                args["confirm_delete"] = True
        except Exception:  # noqa: BLE001
            pass
        # Approvals OFF in an INTERACTIVE chat run is itself the confirmation.
        #
        # Until now the destructive-delete floor ignored the toggle entirely,
        # on the reasoning that auto-confirming `rm -rf` is a far bigger
        # relaxation than skipping a commit prompt. In practice that made the
        # toggle feel broken: the guard matches the whole command string, so
        # ordinary remote maintenance — `ssh host 'docker rm -f c'`,
        # `ssh host 'kubectl delete pod p'`, `git clean -fdx` — kept prompting
        # after the operator had explicitly turned approvals off. The operator
        # asked for this after seeing exactly that.
        #
        # The relaxation is deliberately narrow, and every other floor stands:
        #   * session_id is None (an AUTONOMOUS ticket run) never reaches here
        #     — approvals_required(None) is True by design, so _mode_approvals
        #     is True and this branch cannot fire. Unattended runs stay guarded.
        #   * it is per chat MODE: turning off Chat does not relax Plan or
        #     Pipeline, each of which has its own toggle.
        #   * a DENY policy still blocks, and the workspace jail, blanket-git
        #     and server-start refusals are untouched.
        # Setting confirm_delete is what carries the decision through to the
        # shell tool's own delete refusal, which would otherwise still fire.
        if not _mode_approvals and _destructive_del and session_id is not None:
            _destructive_del = False
            args["confirm_delete"] = True
            # Audited, not invisible — same rule as the commit bypass.
            _log.warning("chat.delete_auto_confirmed reason=approvals_off "
                        "session=%s cmd=%s", session_id, _cmd[:200])
    return _destructive_del, _auto_commit


def _compute_gate_decision(name, args, cwd, session_id, verdict):
    """Decide whether one tool call must pause for approval: per-mode approval
    toggle, pre-apply forced review, destructive-delete detection, and the
    scoped commit-auto-approve / allow-delete opt-ins (each subordinate floor
    preserved; may set args[confirm_delete]). Returns
    ``(gate, destructive_del, force_review, bypass)`` with ``bypass`` =
    ``(auto_approved, scope)``."""
    from aiforge_core.runtime import chat_approve
    from aiforge_core.runtime.tools import tool_policy
    # Pre-apply review mode (Gap D): when armed for this session, force the
    # approval gate for any mutating tool even if policy would auto-allow.
    _force_review = (session_id is not None and _is_mutating(name, args)
                     and chat_approve.review_edits(session_id))
    # Per-mode approval Settings toggle (Chat/Plan/Pipeline). When ON, this
    # mode pauses for Approve/Reject AND the captured "never re-ask" bypass
    # flags below are IGNORED (the toggle is the master control — a user who
    # turned approvals ON wants to be asked, not silently auto-approved). When
    # OFF, ask-policy/review gates don't fire and the bypass flags apply.
    _mode_approvals = chat_approve.approvals_required(session_id)
    # Destructive delete (rm -rf, etc): the run_command tool has its OWN
    # confirm_delete arg gate (delete_guard). If we don't route it through
    # the approval gate AND mark it confirmed on approve, the tool keeps
    # refusing ("re-issue with confirm_delete=true") and the model loops
    # asking the user to "type yes" forever. So always gate it, and let the
    # human's Approve BE the confirmation.
    _destructive_del, _auto_commit = _command_gate_flags(
        name, args, cwd, session_id, _mode_approvals)
    # review_edits (_force_review) is an EXPLICIT per-request / global opt-in
    # ("hold my edits") — it must gate INDEPENDENTLY of the per-mode approval
    # toggle, else body.review_edits=True / AIFORGE_CHAT_REVIEW_EDITS=1 were
    # silently ignored whenever the (default-OFF) mode toggle was off. Only
    # the ASK-POLICY gate is subordinate to the mode toggle; forced review
    # and destructive deletes always gate.
    _gate = ((verdict["policy"] == tool_policy.ASK and _mode_approvals)
             or _force_review
             or _destructive_del)
    # A captured "commit directly" flag may auto-approve the gate ONLY when
    # the SOLE reason to gate is a pure whole-command git commit/add/push —
    # NEVER when a destructive delete (or any non-commit risk: forced review,
    # DENY) co-occurs. So `git commit && rm -rf` is NOT auto-approved.
    if _gate and _auto_commit and not _destructive_del and not _force_review \
            and verdict["policy"] != tool_policy.DENY:
        _gate = False
        # Audit: emit an attributable record of the bypass (not invisible).
        try:
            from aiforge_core.runtime import rule_capture as _rc2
            _ascope = _rc2.flag_active_scope(
                "commit_auto_approve", repo=_repo_name(cwd),
                session_id=session_id)
        except Exception:  # noqa: BLE001
            _ascope = None
        return _gate, _destructive_del, _force_review, (True, _ascope)
    return _gate, _destructive_del, _force_review, (False, None)


def _autonomous_decision(name, args, _destructive_del):
    """The approve/reject decision for an autonomous run (no human): auto-approve
    caution/review, hard-block only a DANGEROUS command or destructive delete."""
    # Autonomous path (parallel sub-Doer) — no human to approve.
    # Mirror run_shell's floor: auto-approve caution/review gates,
    # hard-block only truly DANGEROUS commands + destructive deletes
    # (a blanket reject here silently broke sudo / -g installs /
    # force-push in worktree-isolated autonomous runs).
    _danger = bool(_destructive_del)
    if not _danger and name in ("run_command", "run_shell", "serve",
                                "bash", "shell", "watch_until"):
        try:
            from aiforge_core.runtime.tools import command_risk
            _lvl = command_risk.assess(
                args.get("cmd") or args.get("command") or "")["level"]
            _danger = _lvl == command_risk.DANGEROUS
        except Exception:  # noqa: BLE001
            _danger = False
    decision = ({"decision": "reject", "note": "autonomous: dangerous action blocked"}
                if _danger else
                {"decision": "approve", "note": "autonomous auto-approve"})
    return decision


def _handle_rejection(name, args, session_id, convo, decision):
    """Handle a rejected/expired approval: reject-with-guidance folds the note in
    as a steer and continues; interactive reject-without-guidance stops and waits
    for the user; autonomous reject records an observation and continues. Returns
    "continue"/"return"."""
    from aiforge_core.runtime import chat_steer
    _rnote = decision.get("note") or ""
    _user_guidance = chat_steer.user_guidance(_rnote)
    result = {"ok": False, "rejected": True,
              "error": "user rejected this action"
                       + (f": {_rnote}" if _rnote else "")}
    yield {"type": "tool", "name": name, "args": args, "result": result}
    # CHAT-ON-APPROVAL: if the user rejected WITH guidance, don't just
    # stop — fold the guidance in as a steer and CONTINUE so the agent
    # adjusts immediately (no separate follow-up message needed).
    if session_id is not None and _user_guidance:
        yield chat_steer.steer_event(_user_guidance)
        convo.append({"role": "user",
                      "content": chat_steer.reject_directive(
                          name, _user_guidance)})
        return "continue"
    # Interactive reject WITHOUT guidance is TERMINAL: STOP and WAIT
    # for the user (the old record-and-continue let a model that
    # didn't emit ASK: just keep going). Pause via awaiting_input;
    # the next user message resumes. Autonomous runs (session_id is
    # None) keep the record-and-continue behaviour.
    if session_id is not None:
        _ask = ("Stopped — you rejected the "
                f"`{name}` action"
                + ". Tell me what you'd like me to do instead, and "
                "I'll continue from there.")
        yield {"type": "message", "awaiting_input": True, "text": _ask}
        yield {"type": "done"}
        return "return"
    convo.append({"role": "user",
                  "content": f"OBSERVATION: {json.dumps(result)} "
                             "(the user rejected it — do NOT retry; "
                             "adjust or ASK what they want instead.)"})
    return "continue"

def _run_approval(name, args, cwd, session_id, convo, verdict, _destructive_del):
    """Surface the diff preview + Approve/Reject for a gated tool call and wait
    (autonomous runs auto-approve caution, hard-block only DANGEROUS/destructive;
    a Stop landing while the gate is open re-checks before dispatch). Mutates
    ``args``/``convo``. Returns "continue"/"return"/None."""
    from aiforge_core.runtime import chat_approve, chat_cancel
    from aiforge_core.runtime.tools import tool_policy
    # Approval gate (#1): surface the action + diff preview, block on
    # the user's Approve/Reject (POST /api/chat/sessions/{id}/approve).
    preview = _diff_preview(name, args, cwd)
    seq = chat_approve.request(session_id) if session_id is not None else 0
    if verdict["policy"] == tool_policy.ASK:
        _reason = verdict["reason"]
    elif _destructive_del:
        _reason = "Confirm this destructive delete before it runs."
    else:
        _reason = "Review edits: confirm this file change before it lands."
    yield {"type": "approval", "id": seq, "name": name, "args": args,
           "reason": _reason, "preview": preview}
    if session_id is None:
        decision = _autonomous_decision(name, args, _destructive_del)
    else:
        decision = chat_approve.wait(session_id)
    # M4: a gate left unanswered (user navigated away) auto-rejects on
    # timeout — surface it explicitly so the UI shows "approval expired"
    # instead of silently moving on with a rejected action.
    if decision.get("note") == "approval timed out":
        yield {"type": "approval_expired", "id": seq, "name": name}
    if decision.get("decision") != "approve":
        return (yield from _handle_rejection(
            name, args, session_id, convo, decision))
    # Approved → the human's Accept IS the delete confirmation, so
    # satisfy the run_command tool's confirm_delete gate (otherwise it
    # re-refuses and the model loops asking the user again).
    if _destructive_del:
        args["confirm_delete"] = True
    # A Stop that landed WHILE the approval gate was open must not still
    # write the file — the file tools have no subprocess for cancel() to
    # kill, so re-check here before dispatching the (now-approved) tool.
    if session_id is not None and chat_cancel.is_cancelled(session_id):
        yield {"type": "tool", "name": name, "args": args,
               "result": {"ok": False, "error": "cancelled"}}
        # continue (not break) → the top-of-loop cancel check emits the
        # accurate "stopped by user" rather than the safety-cap message.
        return "continue"
    return None


def _approval_gate(name, args, cwd, session_id, convo):
    """Permission + approval gate for one tool call. Returns "continue"/"return"
    to steer the caller's loop, or None to proceed to dispatch."""
    from aiforge_core.runtime.tools import tool_policy
    # Permission policy (#5) + risk (#7): allow / ask / deny.
    verdict = tool_policy.decide(name, args)
    if verdict["policy"] == tool_policy.DENY:
        result = {"ok": False, "blocked": "policy",
                  "error": f"'{name}' is denied by policy: {verdict['reason']}"}
        yield {"type": "tool", "name": name, "args": args, "result": result}
        convo.append({"role": "user",
                      "content": f"OBSERVATION: {json.dumps(result)}"})
        return "continue"
    _gate, _destructive_del, _force_review, _bypass = _compute_gate_decision(
        name, args, cwd, session_id, verdict)
    if _bypass[0]:
        yield {"type": "auto_approved", "name": name,
               "flag": "commit_auto_approve", "scope": _bypass[1]}
    if _gate:
        _sig = yield from _run_approval(
            name, args, cwd, session_id, convo, verdict, _destructive_del)
        if _sig is not None:
            return _sig
    return None


def _may_extend(st, n):
    """True when this turn EARNED another budget: extensions remain and it
    produced NEW work (a landed edit, or a read of something not read before)
    since the last one. Both counters are monotonic, so a spinning agent leaves
    the mark unchanged and is stopped. Consumes one extension when True."""
    if st.granted_at_step == n:
        return True
    if st.extensions_used >= st.ext_budget:
        return False
    _fp = _worktree_fingerprint(st.cwd) if st.wt_fp0 else ""
    mark = (st.reads_new, st.edits_made, _fp)
    if mark == st.progress_mark:
        return False
    st.extensions_used += 1
    st.progress_mark = mark
    st.granted_at_step = n
    return True


def _cap_stop_reason(st):
    """The user-facing reason string when the step cap stops a run, naming the
    knob that actually applied (Quick max_steps / unattended cap / safety cap)."""
    if st.caller_cap is not None:
        _why = (f"(stopped: used up Quick mode's {st.safety}-step "
                "budget — send it again with Quick off, or raise "
                "AIFORGE_CHAT_QUICK_STEPS)")
    elif st.unattended:
        # The operator may have set the step cap to 0; saying
        # "raise the step cap" would send them to a knob that is
        # already off and had no say in this stop.
        _why = (f"(stopped: hit the {st.safety}-step cap for runs with "
                "nobody watching — raise the background step cap in "
                "Settings → Agent limits, or "
                "AIFORGE_CHAT_UNATTENDED_CAP)")
    else:
        _why = ("(stopped: hit the runaway safety cap — raise the "
                "step cap in Settings → Agent limits, or "
                "AIFORGE_CHAT_SAFETY_CAP; 0 = no limit — if this "
                "was real work)")
    return _why


def _step_cap_guard(st, n):
    """Runaway step-cap check: a turn still producing new work extends its step
    budget (after a forced condense); otherwise stop, naming the knob that
    actually applied (Quick / unattended / safety cap). Returns "return" to end
    the turn, or None to continue."""
    if st.capped and n > st.safety:
        # Runaway step cap. Before giving up, offer an extension to a turn
        # that is still producing new work — condense the history first so
        # the extra steps run in a clean window rather than a bloated one.
        if _may_extend(st, n):
            st.safety += st.cap_base
            _before_ext = len(st.convo)
            st.convo = _compact_convo(st.convo, keep_recent=8, role=st.role,
                                   complete_fn=st.complete_fn,
                                   session_id=st.session_id, force=True)
            if len(st.convo) < _before_ext:
                st.read_sigs_seen.clear()   # results dropped → re-reads are valid
            _did = ("condensed the history and " if len(st.convo) < _before_ext
                    else "")
            yield {"type": "thought", "role": "system",
                   "text": f"⏳ still making progress — {_did}extended the "
                           f"step budget to {st.safety} "
                           f"({st.extensions_used}/{st.ext_budget})"}
        else:
            _fire_stop("cap", st.cwd)
            # Name the knob that ACTUALLY stopped this run. A Quick-mode
            # turn is bounded by its caller's max_steps, so pointing the
            # user at the Settings cap sends them to a number that had no
            # say — and with the cap set to 0 that number is already off.
            _why = _cap_stop_reason(st)
            yield {"type": "message", "text": _why}
            yield {"type": "done"}
            return "return"
    return None


def _deadline_guard(st, n):
    """Wall-clock turn-deadline check: a turn still landing new work buys another
    time slice (after a forced condense); otherwise stop. Returns "return" to end
    the turn, or None to continue."""
    if st.turn_deadline is not None and time.monotonic() > st.turn_deadline:
        # Same deal as the step cap: a turn still landing new work buys
        # another slice of wall clock instead of losing everything.
        if _may_extend(st, n):
            st.turn_deadline = time.monotonic() + st.turn_budget_s
            _before_ext = len(st.convo)
            st.convo = _compact_convo(st.convo, keep_recent=8, role=st.role,
                                   complete_fn=st.complete_fn,
                                   session_id=st.session_id, force=True)
            if len(st.convo) < _before_ext:
                st.read_sigs_seen.clear()
            _did = ("condensed the history and " if len(st.convo) < _before_ext
                    else "")
            yield {"type": "thought", "role": "system",
                   "text": f"⏳ still making progress — {_did}extended the "
                           f"turn by {int(st.turn_budget_s)}s "
                           f"({st.extensions_used}/{st.ext_budget})"}
        else:
            _fire_stop("deadline", st.cwd)
            yield {"type": "message",
                   "text": f"(stopped: hit the {int(st.turn_budget_s)}s turn "
                           "time budget — raise the turn deadline in "
                           "Settings → Agent limits (or "
                           "AIFORGE_CHAT_TURN_DEADLINE_S) if this was real "
                           "long-running work)"}
            yield {"type": "done"}
            return "return"
    return None


def _append_directive(st, _directive):
    """Append a steer/reject directive as a user turn — merged into a trailing
    user turn (list-safe for a vision turn) to avoid two consecutive user turns
    that break some providers, else a fresh turn."""
    # If the last turn is already a user message (e.g. the
    # OBSERVATION we just appended after a tool step), MERGE the
    # steer into it — two consecutive user turns break some
    # providers (claude_local). Otherwise append a fresh user turn.
    _last = st.convo[-1] if st.convo else None
    if _last is not None and _last.get("role") == "user":
        _c = _last.get("content")
        if isinstance(_c, list):
            # A VISION turn's content is a list of parts. `+=` on a
            # list extends it with the string's CHARACTERS: one
            # steer became 364 single-character parts, invisible to
            # _text_of (so the condenser and the context meter both
            # missed it) while still being sent.
            _c.append({"type": "text", "text": _directive})
        else:
            _last["content"] = f"{_c or ''}\n\n{_directive}"
    else:
        st.convo.append({"role": "user", "content": _directive})

def _drain_steering(st, session_id):
    """Fold any user-injected mid-run steering/rejections into the working
    context as ONE user turn before the next model call (merging into a trailing
    user turn to avoid two-in-a-row), and surface each as a steer event."""
    from aiforge_core.runtime import chat_interject
    # Mid-run steering (Gap A): fold any user-injected guidance into the
    # working context as a user turn BEFORE the next model call, so the
    # agent adjusts course without a Stop + new turn. Surface it so the UI
    # shows the steer was applied.
    if session_id is not None:
        _items = chat_interject.drain_items(session_id)
        if _items:
            from aiforge_core.runtime import chat_steer
            _steers = [t for k, t in _items if k != "reject"]
            _rejects = [t for k, t in _items if k == "reject"]
            # ONE block for everything that drained together, so three
            # queued messages cannot each claim to be the latest.
            _parts = ([chat_steer.steer_block(_steers)] if _steers else [])
            _parts += [chat_steer.reject_note(g) for g in _rejects]
            _directive = "\n\n".join(p for p in _parts if p)
            _append_directive(st, _directive)
            for _k, _t in _items:
                yield chat_steer.steer_event(_t)

def _condense_and_report(st, role, complete_fn, session_id, _meter):
    """Auto-condense the running history to stay within the window (clearing the
    duplicate-read guard + notifying once when it fires), then emit the context-
    fullness + LLM-request usage snapshot."""
    # Auto-condense the running history before the call so a long session
    # can't overflow the model's context window (MUST). Tell the user it
    # happened (one-time per condense) for transparency.
    _before = len(st.convo)
    st.convo = _compact_convo(st.convo, role=role, complete_fn=complete_fn,
                           session_id=session_id)
    if len(st.convo) < _before:
        # The dropped turns took their tool RESULTS with them, so a read
        # whose output is no longer in the window is no longer a duplicate.
        # Without this the guard tells the model "you already ran this, its
        # result is above" about content the condense just deleted — and the
        # turn can never recover the file it is being refused.
        st.read_sigs_seen.clear()
    if len(st.convo) < _before and not st.condensed_notified:
        st.condensed_notified = True   # notify ONCE, not every over-budget turn
        yield {"type": "thought", "role": "system",
               "text": "⚙ condensed earlier context to stay within the window"}
    # M3: surface how full the context window is (char-estimate; ~4 chars/
    # token) so the user can see they're approaching the condense point.
    # MUST mirror _compact_convo's math exactly (history-only sum vs a
    # budget that reserves the ACTUAL system prompt, list-safe _text_of) —
    # the old raw-len/whole-convo version double-counted the per-turn
    # system prompt against a 14K estimate, so the meter jumped between
    # turns and collapsed to ~0 on image turns.
    _sys_len = (len(_text_of(st.convo[0]))
                if st.convo and st.convo[0].get("role") == "system" else 0)
    _ctx_chars = sum(len(_text_of(m)) for m in st.convo[1:])
    _ctx_budget = _ctx_budget_chars(role, sys_chars=_sys_len)
    if _ctx_budget > 0:
        # ~4 chars/token → surface ABSOLUTE token counts (in k) alongside the
        # pct so the UI can show "120k / 256k" not just a bare percentage.
        _ctx_tokens = _ctx_chars // 4
        _win_tokens = _ctx_budget // 4
        _calls = _meter.snapshot(session_id) if _meter is not None else {}
        yield {"type": "usage", "context_chars": _ctx_chars,
               "budget_chars": _ctx_budget,
               "context_tokens": _ctx_tokens,
               "window_tokens": _win_tokens,
               "pct": min(100, round(_ctx_chars * 100 / _ctx_budget)),
               # Requests actually sent to the LLM — this turn, this chat,
               # and the machine-wide rate. "Why is one question 40 calls?"
               "llm_turn": _calls.get("turn", 0),
               "llm_session": _calls.get("session", 0),
               "llm_per_min": _calls.get("per_minute", 0),
               # How many of those requests came back with nothing. A
               # subset of llm_turn, not an extra: "12 requests, 7 failing"
               # is a retry storm, "12 requests" alone looks like a
               # thorough turn.
               "llm_turn_failed": _calls.get("turn_failed", 0),
               "llm_failed_per_min": _calls.get("failed_per_minute", 0),
               # Tokens the model has WRITTEN for this message so far,
               # as the provider reported them.
               "llm_turn_tokens_out": _calls.get("turn_tokens_out", 0)}

def _verify_on_final(st, cwd, plan_mode, builder):
    """Progress-gated verify->fix on FINAL: only an act-mode run that edited
    files with a real suite; loop while failures drop, else accept honestly.
    Returns continue or None."""
    # A + B: enforced verify→fix on FINAL (progress-gated). Only for an
    # act-mode run that actually EDITED files with a real test suite —
    # a Q&A turn (0 edits) or read-only plan mode is untouched. Keep
    # looping while the failure count DROPS; once it stalls (2 rounds no
    # improvement) accept the HONEST still-failing final rather than
    # churn. This gives simple/doer runs the pipeline's no-false-green
    # guarantee. Opt out: AIFORGE_CHAT_VERIFY_ON_FINAL=0.
    if (not plan_mode and not builder and st.edits_made > 0
            and st.verify_rounds < _verify_max_rounds()
            and _verify_on_final_enabled()):
        _vok, _vout = _run_project_verify(cwd)
        if _vok is False:
            try:
                from aiforge_core.runtime.parallel_subtasks import _fail_count
                _fails = _fail_count(_vout)
            except Exception:  # noqa: BLE001
                _fails = 1
            if st.verify_prev_fails is not None and _fails >= st.verify_prev_fails:
                st.verify_stalls += 1
            else:
                st.verify_stalls = 0
            st.verify_prev_fails = _fails
            if st.verify_stalls < 2:
                st.verify_rounds += 1
                yield {"type": "thought", "role": "system",
                       "text": f"✗ tests failing ({_fails}) — fixing "
                               f"(verify round {st.verify_rounds}/"
                               f"{_verify_max_rounds()})…"}
                st.convo.append({"role": "user",
                              "content": _verify_fix_message(_vout)})
                return "continue"
            yield {"type": "thought", "role": "system",
                   "text": f"⚠ tests still failing ({_fails}) after "
                           f"{st.verify_rounds} fix rounds — stopping with "
                           "the honest state."}
    return None


def _claim_guard(st, step, cwd, readonly_mode, builder, _wt_fp0):
    """Claim-vs-reality guard: when the model claims edits but landed zero and
    the tree is unchanged, nudge it to write (bounded); on the last try prepend
    an honest disclaimer. Returns continue or None."""
    # Claim-vs-reality guard: the model asserts it edited/created files
    # but landed ZERO edits this turn AND the working tree is unchanged
    # (checked against every tool + any on-disk write, not just counted
    # ones) — a hallucinated tool-use surfaced as prose (the frequent
    # "I applied the fix to X / Confirmed Fixes Applied" with no diff).
    # Nudge it to actually write (bounded); if it still won't, prepend an
    # honest note so the user is never told a change landed that didn't.
    # Opt out: AIFORGE_CHAT_EDIT_CLAIM_GUARD=0.
    # Disk cross-check: "" = no git signal (honor the contract — NOT
    # "clean"), so in a non-git workspace we rely on _edits_made==0 alone;
    # with git, fire only when the tree is UNCHANGED (a real write would
    # have dirtied it — an incidental dirty tree suppressing the guard is
    # an accepted conservative miss).
    _wt_now = (_worktree_fingerprint(cwd)
               if _edit_claim_guard_enabled() else "")
    _no_landed_write = (_wt_now == "" or _wt_now == _wt_fp0)
    if (not readonly_mode and not builder and st.edits_made == 0
            and _edit_claim_guard_enabled()
            and _claims_file_edits(step.get("text") or "")
            and _no_landed_write):
        if st.edit_claim_nudges < 2:
            st.edit_claim_nudges += 1
            if step.get("text"):
                yield {"type": "thought", "text": step["text"]}
            yield {"type": "thought", "role": "system",
                   "text": "⚠ you described file edits but no write ran "
                           "and nothing changed on disk — applying for "
                           "real…"}
            st.convo.append({"role": "user", "content": _edit_claim_nudge()})
            return "continue"
        step["text"] = _edit_claim_disclaimer(step.get("text") or "")
    return None


def _final_nudges(st, step, builder, strict_finish, _asks):
    """Pre-accept FINAL nudges: builder-not-finalized reminder, implicit-final
    doer nudge (strict_finish), and the one-time multi-ask completeness gate.
    Returns continue to loop again, or None to proceed."""
    # In a builder session, a "final" BEFORE the finalize tool succeeded
    # means the model narrated/stalled ("let me test what's happening…")
    # instead of building the artifact — don't end the interview with
    # nothing created. Nudge it to call the finalize tool and continue the
    # loop (bounded so a model that truly can't finalize still exits).
    if builder and not st.builder_finalized and st.builder_final_tries < 2:
        st.builder_final_tries += 1
        _fin = _BUILDER_FINALIZE_TOOL.get(builder, _THE_FINALIZE_TOOL)
        if step.get("text"):
            yield {"type": "thought", "text": step["text"]}
        st.convo.append({"role": "user", "content":
            f"[system reminder] You stopped without creating the {builder}. "
            f"Call `{_fin}` NOW with the collected values to finish — do "
            f"not just narrate or 'test'. If ONE required value is genuinely "
            f"missing, ask only for that, then finalize."})
        return "continue"
    # Doer guard: an IMPLICIT final (bare prose, no explicit `FINAL:`
    # marker) from a work-producing run (strict_finish — the text-doer /
    # subtask path) is almost always premature narration ("let me test…"),
    # not a real answer. Nudge to act/finish instead of ending with no work.
    # Bounded by continue_nudges so a model that truly can't finish still
    # exits. Interactive chat / generic callers (strict_finish=False) keep
    # bare prose as the legitimate answer — unchanged.
    if step.get("implicit") and strict_finish and not builder:
        st.continue_nudges += 1
        if st.continue_nudges <= 2:
            if step.get("text"):
                yield {"type": "thought", "text": step["text"]}
            st.convo.append({"role": "user", "content":
                "You narrated but did NOT emit an ACTION or an explicit "
                "`FINAL:` line. Continue: take the next ACTION (tool call) "
                "to make progress, or output `FINAL: <answer>` ONLY when "
                "the work is actually done. Do not just narrate or 'test'."})
            return "continue"
    # Multi-ask completeness gate (once): before accepting FINAL on a
    # multi-part message, make the model self-check its answer against
    # the checklist — the #1 simple-mode complaint is answering ask 1
    # and silently dropping the rest.
    if _asks and not st.multiask_checked and not builder:
        st.multiask_checked = True
        yield {"type": "thought", "role": "system",
               "text": f"✔ checking all {len(_asks)} parts of the "
                       "request are addressed…"}
        st.convo.append({"role": "user", "content":
            "[completeness check — not the user] The user's message "
            f"contained {len(_asks)} distinct asks:\n"
            + "\n".join(f"{i + 1}. {a}" for i, a in enumerate(_asks))
            + "\nRe-read your answer above. If EVERY ask is addressed, "
            "resend it unchanged as FINAL. If any is missing, do the "
            "missing work now (ACTIONs as needed) and produce ONE "
            "complete FINAL covering all parts, numbered."})
        return "continue"
    return None


def _is_clean_tree(cwd) -> bool:
    """True only when ``cwd`` IS a git repo AND has nothing uncommitted.

    Deliberately NOT ``_worktree_fingerprint(cwd) == ""``: that helper returns
    "" for a clean tree *and* for "not a git repo / git unavailable", and its
    own docstring warns that "" means "no signal", never "clean". Reusing it
    here would let a tier-2 prediction act automatically in a directory with no
    undo at all — precisely the case the clean-tree rule exists to exclude.

    Anything unclear answers False, which costs an offer instead of an action.
    """
    if not cwd:
        return False
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(cwd),
                             capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001 — a git hiccup must never break the turn
        return False
    return out.returncode == 0 and not out.stdout.strip()


def _turn_summary(st) -> str:
    """One line naming what this turn actually did, for the prediction prompt.

    Read off ``action_counts``, which the loop already maintains — the
    alternative is a second tally that drifts from the first.
    """
    try:
        counts = getattr(st, "action_counts", None) or {}
        names = [str(k) for k, v in counts.items() if v]
    except Exception:  # noqa: BLE001
        return ""
    return ", ".join(names[:8])


def _last_user_message(st) -> str:
    try:
        for m in reversed(list(getattr(st, "convo", []) or [])):
            if isinstance(m, dict) and m.get("role") == "user":
                # _text_of takes the whole MESSAGE, not its content: it handles
                # the multimodal list form a vision turn rewrites content into.
                return _text_of(m)[:2000]
    except Exception:  # noqa: BLE001 — a malformed convo predicts nothing
        return ""
    return ""


def _predict_next_step(message: str, did: str, cwd):
    """The prediction, or None. Split out so a test can replace exactly this.

    The kill switch is honoured BEFORE the context is gathered. ``_is_clean_tree``
    shells out to git on every turn end, so building the argument first meant a
    disabled feature still paid for a subprocess per turn — "off" has to mean
    it costs nothing, not merely that it emits nothing.
    """
    from aiforge_core.runtime import next_step
    from aiforge_core.runtime.next_step import _predict as _np

    if _np._disabled():
        return None
    return next_step.predict({"message": message, "did": did,
                              "repo": _repo_name(str(cwd or "")),
                              "clean_tree": _is_clean_tree(cwd)})


def _emit_suggestion(message: str, did: str, cwd):
    """Yield at most one ``suggestion`` event. Never raises.

    Emitted AFTER the answer and before ``done`` — the same ordering
    ``plan_ready`` uses. The user reads what they asked for either way, so a
    prediction that is slow, wrong or broken costs them nothing.
    """
    try:
        p = _predict_next_step(message, did, cwd)
    except Exception as exc:  # noqa: BLE001 — a prediction never breaks a turn
        _log.debug("next_step: prediction skipped: %s", exc)
        return
    if p is not None:
        yield p.as_event()


def _handle_final(st, step, builder, strict_finish, plan_mode, readonly_mode,
                  cwd, _asks, _wt_fp0):
    """Handle a FINAL step: builder-not-finalized nudge, implicit-final doer
    nudge, multi-ask completeness gate, claim-vs-reality guard, and the
    progress-gated verify→fix loop — then accept (fire stop + emit the answer).
    Returns "continue"/"return"."""
    _sig = yield from _final_nudges(st, step, builder, strict_finish, _asks)
    if _sig == "continue":
        return "continue"
    _sig = yield from _claim_guard(st, step, cwd, readonly_mode, builder, _wt_fp0)
    if _sig == "continue":
        return "continue"
    _sig = yield from _verify_on_final(st, cwd, plan_mode, builder)
    if _sig == "continue":
        return "continue"
    # FINAL accepted on a multi-part turn: close out the tracker so
    # the dock never ends with stale pending items the model forgot
    # to flip.
    if _asks:
        for _i in range(len(_asks)):
            yield {"type": "subtask_update",
                   "slug": f"part-{_i + 1}", "status": "done"}
    _fire_stop("final", cwd)
    yield {"type": "message", "text": _strip_reasoning_prefix(step["text"])}
    yield from _emit_suggestion(_last_user_message(st), _turn_summary(st), cwd)
    yield {"type": "done"}
    return "return"


def _stuck_output_guard(st, out):
    """Stuck-output guard: on N identical model replies, first recover with a
    progress recap + nudge (bounded); if it keeps repeating, stop and ask the
    user. Returns continue/return/None."""
    # Stuck-output loop: identical model reply N times running. A local model
    # deep in a long tool chain (esp. a many-file read sweep) loses track and
    # re-emits an action it already ran — so FIRST recover with a progress
    # recap + "do the NEXT step" nudge (bounded); only give up if that keeps
    # failing. The old hard bail here discarded all the work done so far.
    st.recent_outputs.append(out.strip())
    if (len(st.recent_outputs) == _OUTPUT_REPEAT
            and len(set(st.recent_outputs)) == 1):
        if st.stuck_recoveries < _stuck_recovery_max():
            st.stuck_recoveries += 1
            st.recent_outputs.clear()          # fresh slate for the recovered plan
            _recap = _progress_recap(st.convo)
            yield {"type": "thought", "role": "system",
                   "text": "↺ repeated output — recap + nudge to continue"}
            # Append the repeated assistant turn BEFORE the nudge — else two
            # consecutive user turns (the prior OBSERVATION + this nudge)
            # break providers like claude_local.
            st.convo.append({"role": "assistant", "content": out})
            st.convo.append({"role": "user", "content":
                "[loop guard — not the user] You repeated the SAME output — "
                "that makes no progress. "
                + (_recap + ". " if _recap else "")
                + "Take the NEXT, DIFFERENT step now: act on something not yet "
                "done (e.g. the next unread file), or output `FINAL: <answer>` "
                "if the task is fully complete. Do NOT repeat a previous action."})
            return "continue"
        yield {"type": "message", "awaiting_input": True,
               "text": "I seem to be going in circles on this. Could you "
                       "clarify what you'd like me to do, or give a bit "
                       "more detail? (I stopped rather than keep retrying "
                       "the same thing.)"}
        yield {"type": "done"}
        return "return"

    return None


def _handle_continue_step(st, step, builder, cwd):
    """Handle a continue step (narrated-no-action, or empty_final signalled
    completion with no answer): nudge appropriately (bounded), else stop cleanly.
    Returns continue/return."""
    # Two shapes land here. (a) The model narrated a next step
    # (THOUGHT) but emitted no ACTION — truncated turn or dropped
    # protocol line. (b) reason="empty_final": it SIGNALLED completion
    # and wrote no answer, which used to publish the marker itself as
    # the reply ("ACTION: FINAL" in the chat). Both are nudged; the
    # wording differs because the missing thing differs.
    _empty_final = step.get("reason") == "empty_final"
    if step.get("thought"):
        yield {"type": "thought", "text": step["thought"]}
    st.continue_nudges += 1
    if st.continue_nudges > 2:
        # It keeps not delivering — stop cleanly rather than loop to
        # the safety cap.
        _fire_stop("no_action", cwd)
        if _empty_final:
            # Deliberately NOT "I finished the work": zero tools may
            # have run, and text_doer / analysis_pipeline treat a
            # message that does NOT start with "(stopped:" as a clean
            # outcome — so claiming completion here would poison the
            # pipeline's own quality record. Deliberately no
            # _progress_recap either: that is model-facing text, and it
            # tallies the FINAL markers themselves.
            yield {"type": "message", "text":
                   "(stopped: I signalled I was done but never wrote "
                   "the reply. Ask me to summarise what happened and "
                   "I'll write it up.)"}
        else:
            yield {"type": "message",
                   "text": (step.get("thought") or "").strip()
                   or "I described a next step but couldn't complete the "
                      "action. Could you rephrase or narrow the request?"}
        yield {"type": "done"}
        return "return"
    if _empty_final and builder:
        # A builder session's "answer" is an ARTIFACT: it must call its
        # finalize tool. Telling it "reply with FINAL, do not emit
        # ACTION" is the exact opposite instruction, and the turn would
        # end claiming success with nothing created.
        _fin = _BUILDER_FINALIZE_TOOL.get(builder, _THE_FINALIZE_TOOL)
        st.convo.append({"role": "user", "content":
                      f"You signalled you were finished but never called "
                      f"`{_fin}`, so nothing was created. Call `{_fin}` NOW "
                      f"with the values you have collected."})
    elif _empty_final:
        # The work is done; what is missing is the reply. "Emit an
        # ACTION" is the wrong instruction for that.
        st.convo.append({"role": "user", "content":
                      "You signalled you were finished but wrote no "
                      "answer — the user saw nothing. Reply now with "
                      "`FINAL: <answer>` where <answer> tells them what "
                      "you did and what it means for their request, in "
                      "plain prose. Do not emit ACTION, THOUGHT or any "
                      "other marker."})
    else:
        st.convo.append({"role": "user",
                      "content": "You described your next step but did NOT "
                      "emit an ACTION. Continue now — output the next ACTION "
                      "(tool call) to make progress, or `FINAL: <answer>` if "
                      "you are genuinely done. Do not just narrate."})
    # NOT `n += 1`: the loop head already counted this iteration, and
    # the sibling implicit-final nudge does not double-charge either.
    # On a 6-step Quick turn the double charge turned one nudge into a
    # "used up Quick mode's step budget" stop.
    return "continue"


def _action_stall_guard(st, name, args, sig, _long_chain_help):
    """Stall guard for an action: short-circuit a duplicate read with a progress
    recap, and on the same tool+args repeated too often first recover with a
    recap+nudge (bounded), else pause for the user. Returns continue/return/None."""
    # Duplicate-READ short-circuit: a local model on a long sweep re-issues a
    # read it already ran (its result is still above in the convo). Don't
    # re-execute or count it toward the stuck guard — hand back a cheap
    # progress recap that points at the next unread file / the write step, so
    # every read must make NEW progress. Cleared on any edit (a file just
    # written is worth re-reading). Disabled with the same env switch as the
    # recovery nudge (AIFORGE_CHAT_STUCK_RECOVERIES=0 → full legacy behaviour).
    if _long_chain_help and name in _READ_OBS_TOOLS and sig in st.read_sigs_seen:
        _recap = _progress_recap(st.convo)
        yield {"type": "thought", "role": "system",
               "text": f"⏭ duplicate read skipped ({name})"}
        st.convo.append({"role": "user", "content":
            "OBSERVATION: [skipped — duplicate] You ALREADY ran this exact "
            "read; its result is above and re-reading wastes a step. "
            + (_recap + ". " if _recap else "")
            + "Read a DIFFERENT file you have not read yet, or if you have "
            "enough, WRITE your output now (file_write) or emit FINAL."})
        return "continue"
    # Bounded, LEAST-RECENTLY-USED first. An uncapped turn (cap 0) removes
    # the 2000-step ceiling that used to bound this table in practice, and
    # nothing else prunes it (convo is condensed, recent_outputs is a maxlen
    # deque, read_sigs_seen is cleared on compaction).
    #
    # The order matters more than the bound: a plain dict is ordered by
    # FIRST insert, so dropping "the oldest half" would evict exactly the
    # signatures that have been repeating longest — the ones accumulating
    # strikes — and the stall guard would never fire on the very runaway
    # this table exists to catch. move_to_end on each touch makes the
    # eviction least-recently-SEEN instead.
    st.action_counts[sig] = st.action_counts.get(sig, 0) + 1
    st.action_counts.move_to_end(sig)
    while len(st.action_counts) > _ACTION_SIG_MAX:
        st.action_counts.popitem(last=False)
    if st.action_counts[sig] >= _LOOP_REPEAT:
        # A local model on a long chain re-issues an action it already ran —
        # most often re-reading a file it read earlier (it lost track over the
        # growing history), which the old hard bail turned into an abandoned
        # task. Recover FIRST: recap what's already done + point at the next
        # step (bounded); only give up if the model keeps repeating.
        if st.stuck_recoveries < _stuck_recovery_max():
            st.stuck_recoveries += 1
            st.action_counts[sig] = 0          # clear this action's strike count
            _recap = _progress_recap(st.convo)
            yield {"type": "thought", "role": "system",
                   "text": f"↺ repeated `{name}` — recap + nudge to continue"}
            st.convo.append({"role": "user", "content":
                f"[loop guard — not the user] You already ran `{name}` with "
                "these exact args and its result is ABOVE — repeating it makes "
                "no progress. "
                + (_recap + ". " if _recap else "")
                + "Do the NEXT, DIFFERENT step now: act on something not yet "
                "done (e.g. the next unread file from the request), or output "
                "`FINAL: <answer>` if everything is complete. Do NOT repeat a "
                "previous action."})
            return "continue"
        yield {"type": "message", "awaiting_input": True,
               "text": f"I keep trying the same step (`{name}`) without "
                       "progress. I've paused — could you clarify or tell "
                       "me how you'd like me to proceed?"}
        yield {"type": "done"}
        return "return"
    return None


def _pre_dispatch_gates(st, name, args, readonly_mode, analyze_mode):
    """Pre-dispatch bookkeeping gates: plan_progress flips a UI subtask (pure
    bookkeeping, allowed in every mode); read-only Plan/Analyze mode blocks a
    mutating tool. Returns continue or None."""
    # Simple-mode task tracker: plan_progress flips a checklist item in
    # the UI's subtasks dock. Pure bookkeeping — no side effects, allowed
    # in every mode (incl. plan), never gated.
    if name == "plan_progress":
        _slug = str(args.get("slug") or args.get("part") or "").strip()
        _st = str(args.get("status") or "done").strip().lower()
        if _st not in ("pending", "running", "done", "failed"):
            _st = "done"
        if _slug:
            yield {"type": "subtask_update", "slug": _slug, "status": _st}
        result = {"ok": bool(_slug), "slug": _slug, "status": _st,
                  **({} if _slug else {"error": "missing 'slug'"})}
        yield {"type": "tool", "name": name, "args": args, "result": result}
        st.convo.append({"role": "user",
                      "content": f"OBSERVATION: {json.dumps(result)}"})
        return "continue"

    # PLAN/ANALYZE mode (#2): block mutating tools — read-only only.
    if readonly_mode and name not in _READONLY_TOOLS:
        _mname = "Analyze" if analyze_mode else "Plan"
        _mtail = ("Report your FINDINGS." if analyze_mode
                  else "Finish with a PLAN; the user will switch to Act "
                       "mode to execute it.")
        result = {"ok": False, "blocked": "plan_mode",
                  "error": f"'{name}' is blocked in {_mname} mode "
                           f"(read-only). {_mtail}"}
        yield {"type": "tool", "name": name, "args": args, "result": result}
        st.convo.append({"role": "user",
                      "content": f"OBSERVATION: {json.dumps(result)}"})
        return "continue"
    return None


def _pre_tool_checks(st, name, args, cwd, _scope_globs):
    """PreToolUse hook block + autonomous scope-allowlist enforcement. Yields a
    scope-violation observation and returns continue when the path is out of
    scope; otherwise returns the hook-block dict (or None) for dispatch."""
    # Lifecycle hook (Claude Code parity): PreToolUse can block a tool
    # (a `block_on_nonzero` hook that exits non-zero) — surface it like the
    # plan-mode/policy blocks. Hooks soft-fail; a hooks error never breaks
    # the turn.
    _hook_block = None
    try:
        from aiforge_core.runtime import hooks as _hooks
        _pre = _hooks.fire("PreToolUse", {"tool": name, "args": args}, cwd)
        if _pre.get("blocked"):
            _hook_block = _pre
    except Exception:  # noqa: BLE001 — hooks must never break dispatch
        _hook_block = None

    # Workspace jail (on by default, AIFORGE_CHAT_WORKSPACE_JAIL=0 opts out).
    # The session's cwd
    # is otherwise only a DEFAULT: an absolute path in a mutating file tool
    # writes anywhere. Refuse WITHOUT writing and tell the model why, so an
    # off-topic recall can never turn into an edit in a repo the user never
    # mentioned in this chat.
    try:
        from aiforge_core.runtime import scope_guard as _sg_jail
        _jailed = _sg_jail.outside_workspace(name, args or {}, cwd)
    except Exception:  # noqa: BLE001 — never break dispatch
        _jailed = []
    if _jailed:
        result = {
            "ok": False, "error": "outside_workspace",
            "blocked_paths": _jailed, "workspace": cwd,
            "hint": ("Write refused: the path is outside this session's "
                     f"workspace ({cwd}). Write inside it, or ask the user to "
                     "point this chat at that project. If writing there is "
                     "genuinely intended, the operator can set "
                     "AIFORGE_CHAT_WORKSPACE_JAIL=0 — do not assume it; ask."),
        }
        yield {"type": "tool", "name": name, "args": args, "result": result}
        st.convo.append({"role": "user",
                         "content": f"OBSERVATION: {json.dumps(result)}"})
        return "continue"

    # Scope allowlist enforcement (autonomous Doer path). Reject a
    # mutating file tool whose resolved target path is outside the
    # ticket's scope_allowlist_globs — refuse WITHOUT writing, and hand
    # the model a corrective observation. Reuses scope_guard's matcher
    # so the text path enforces exactly like the native callback.
    if _scope_globs:
        try:
            from aiforge_core.runtime import scope_guard as _sg
            _off = [p for p in _sg._path_from_args(name, args or {})
                    if not _sg._matches_any(p, _scope_globs)]
        except Exception:  # noqa: BLE001 — never break dispatch
            _off = []
        if _off:
            result = {
                "ok": False, "error": "scope_violation",
                "blocked_paths": _off,
                "scope_allowlist_globs": _scope_globs,
                "hint": ("Edit refused: path is outside the ticket's "
                         "scope_allowlist_globs. Edit only files inside "
                         "an allowed glob."),
            }
            yield {"type": "tool", "name": name, "args": args,
                   "result": result}
            st.convo.append({"role": "user",
                          "content": f"OBSERVATION: {json.dumps(result)}"})
            return "continue"
    return _hook_block


def _record_edit(st, name, args, result, cwd):
    """When an edit tool landed: bump the edit counter, invalidate the duplicate-
    read guard, and run a post-edit syntax self-check that surfaces + feeds back
    any error this step. Yields the syntax-warning events."""
    if name in _EDIT_TOOL_NAMES and not (
            isinstance(result, dict) and result.get("ok") is False):
        st.edits_made += 1
        st.read_sigs_seen.clear()   # a file just changed → re-reads are valid again
        # D: post-edit self-check. Immediately syntax-check the file just
        # written and, if broken, hand the model the error THIS step (tight
        # feedback) instead of letting it surface only at the end-of-run test
        # gate. Best-effort + opt-out (AIFORGE_CHAT_POST_EDIT_CHECK=0).
        if os.environ.get("AIFORGE_CHAT_POST_EDIT_CHECK", "1") not in ("0", "false"):
            try:
                _pe = _post_edit_syntax_error(name, args, cwd)
            except Exception:  # noqa: BLE001
                _pe = None
            if _pe:
                yield {"type": "thought", "role": "system",
                       "text": f"⚠ syntax error in the file you just edited "
                               f"— fix it now: {_pe[:160]}"}
                st.convo.append({"role": "user", "content":
                    "[automated syntax check — not the user] The file you just "
                    f"wrote has a syntax error; fix it before continuing:\n{_pe[:600]}"})


def _record_read(st, name, sig, result, _long_chain_help):
    """Count a landed READ as new progress the first time its signature is seen
    (feeds the extension budget), and remember it for the duplicate-read guard."""
    if (name in _READ_OBS_TOOLS and not (
            isinstance(result, dict) and result.get("ok") is False)):
        # F2: counted OUTSIDE the _long_chain_help gate —
        # AIFORGE_CHAT_STUCK_RECOVERIES=0 turns off the duplicate-read
        # NUDGE, and must not silently delete half the progress signal with
        # it (a read-only research turn would never extend on that box).
        if sig not in st.read_sigs_ever:
            st.reads_new += 1        # real progress: knowledge it did not have
            st.read_sigs_ever[sig] = True
            while len(st.read_sigs_ever) > _ACTION_SIG_MAX:
                st.read_sigs_ever.popitem(last=False)
        if _long_chain_help:
            st.read_sigs_seen.add(sig)


def _post_tool(st, name, args, result, cwd, sig, n, _long_chain_help, _bundle):
    """Post-tool bookkeeping: PostToolUse hook, emit the tool result, count landed
    reads/edits (feeding the progress + verify gates), post-edit syntax self-
    check, builder-finalize signal, and append the (smart-truncated) OBSERVATION."""
    # PostToolUse hook (best-effort, never blocks).
    try:
        from aiforge_core.runtime import hooks as _hooks
        _hooks.fire("PostToolUse",
                    {"tool": name, "args": args, "result": result}, cwd)
    except Exception:  # noqa: BLE001 — hooks must never break the turn
        pass
    yield {"type": "tool", "name": name, "args": args, "result": result,
           "call_id": n}
    # Loop-engineering bookkeeping: count edits that actually LANDED (gates
    # the verify-on-final loop — a 0-edit Q&A turn is never test-gated).
    # Remember a successful read so a later identical re-read short-circuits.
    _record_read(st, name, sig, result, _long_chain_help)
    yield from _record_edit(st, name, args, result, cwd)
    # Builder finalize: a successful create_job_script / learn_skill /
    # learn_workflow / remember_rule ends the interview. Signal the UI so it
    # can drop this session's builder mode — otherwise every later message
    # re-fires the charter and the user is stuck building forever (and can be
    # walked into duplicate artifacts).
    if name in _FINALIZE_TOOLS and isinstance(result, dict) and result.get("ok"):
        st.builder_finalized = True
        yield {"type": "builder_done", "kind": name}
    _obs_cap = _MAX_OBS_READ if name in _READ_OBS_TOOLS else _MAX_OBS
    # Content-READ tools: cut oversized documents at a STRUCTURE boundary
    # (chonkie) with a continuation note, instead of a blunt slice that
    # hands the model a broken JSON/sentence tail. Others keep the slice.
    obs = (_smart_truncate_obs(result, _obs_cap)
           if name in _READ_OBS_TOOLS else json.dumps(result)[:_obs_cap])
    # Recency reminder: a strict output format from an APPLICABLE SKILL sits
    # in the system prompt (far above), while this fresh tool result sits at
    # the end where the model attends most — so after a tool round-trip it
    # tends to summarize the result in its own words and drop the format
    # (e.g. a jira-reading skill's exact layout). Re-assert the format right
    # next to the data so the FINAL honours it. Only when a skill fired.
    _tail = ("\n[format reminder] If your FINAL presents this result and an "
             "APPLICABLE SKILL above specifies an output format, reproduce "
             "it EXACTLY — no extra prose, headers, or table it does not "
             "specify.") if _bundle.skills_md else ""
    st.convo.append({"role": "user", "content": f"OBSERVATION: {obs}{_tail}"})

def _run_completion(st, role, complete_fn, session_id, _meter):
    """Run one model completion: bind the per-step meter, call the model (with the
    bounded retry recovery), reset the meter, and normalise the result. Yields
    retry/stop events; returns the completion text, or _RETRY_STOP to end."""
    # This STEP's own send counter, bound for the duration of the step.
    # Not a delta of the session's turn count: that was inert for every
    # caller without a session (jobs, text_doer, the analysis fan-out —
    # the unattended paths where a storm has nobody watching), refundable
    # by a concurrent turn_reset, and spendable by unrelated same-session
    # traffic.
    _step_calls = None
    _step_tok = None
    if _meter is not None and _max_gen_per_step() > 0:
        try:
            _step_calls = _meter.step_begin()
            _step_tok = _meter.step_bind(_step_calls)
        except Exception:  # noqa: BLE001
            _step_calls, _step_tok = None, None
    try:
        out = _complete_cancellable(complete_fn, role, st.convo, session_id)
    except Exception as exc:  # noqa: BLE001
        out = yield from _retry_completion(
            complete_fn, role, st.convo, session_id, exc,
            _step_calls, _meter, _step_tok)
        if out is _RETRY_STOP:
            return _RETRY_STOP
    # The step's sends are counted; unbind before the next one binds its
    # own (a step that leaves its counter bound would have the NEXT step's
    # calls spend a budget that is already exhausted).
    if _meter is not None:
        _meter.step_reset(_step_tok)
        _step_tok = None

    # H1: Stop pressed DURING generation — the cancellable wrapper returned
    # the sentinel (the abandoned LLM call finishes in the background,
    # ignored). Distinct from a legitimately-empty completion below.
    if out is _CANCELLED:
        yield {"type": "error", "text": "stopped by user"}
        yield {"type": "done"}
        return _RETRY_STOP
    if out is None:
        out = ""   # a real empty completion — treat as an empty turn
    return out


def _builder_nudge(st, builder, n):
    """Once a builder session has interviewed enough, inject a one-time reminder
    to call the finalize tool NOW so the session ends with an artifact."""
    # Builder nudge (#7): a local model can interview forever and never emit
    # the finalize tool, leaving the session with no artifact. Once it has had
    # enough back-and-forth, inject a one-time reminder to finalize NOW.
    if builder and not st.builder_nudged and n >= _BUILDER_NUDGE_AFTER:
        st.builder_nudged = True
        _fin = _BUILDER_FINALIZE_TOOL.get(builder, _THE_FINALIZE_TOOL)
        st.convo.append({"role": "user", "content":
            f"[system reminder] You have gathered enough detail. Call "
            f"`{_fin}` NOW with the collected values to finish — do not keep "
            f"asking questions. If one required value is genuinely missing, "
            f"ask ONLY for that, then finalize."})

def _resolve_complete_fn(complete_fn, role):
    """Resolve the completion fn: when the caller injected none, use the default
    and swap in native OpenAI tool-calling if the model/role supports it. Returns
    (complete_fn, native_on)."""
    _native_on = False
    if complete_fn is None:
        from aiforge_core.llm.client import complete as complete_fn  # type: ignore
        # Native OpenAI tool-calling — the reliable alternative to the text
        # ACTION/ARGS_JSON protocol that local models fumble into `ARGS_JSON: {}`
        # (the same mechanism OpenWebUI uses on these endpoints). When the model
        # supports it (probed once, or forced via AIFORGE_CHAT_TOOL_PROTOCOL),
        # swap in a completion that returns REAL structured args; the rest of the
        # loop is unchanged — it parses the SAME synthesized ACTION step. Only
        # when the caller didn't inject its own complete_fn (tests/doer paths).
        try:
            from ._native import make_native_complete_fn, native_tools_enabled
            if native_tools_enabled(role):
                complete_fn = make_native_complete_fn()
                _native_on = True
        except Exception:  # noqa: BLE001 — native must never break the turn
            pass

    return complete_fn, _native_on


def _compute_caps(max_steps, session_id):
    """Compute the step-cap budget: the operator/default cap, a positive caller
    max_steps override, the unattended fallback, and the effective safety cap.
    Returns (cap_base, caller_cap, unattended, safety, capped)."""
    _cap_base = _safety_cap()
    # Only a POSITIVE max_steps is a caller's budget. 0 keeps its historical
    # meaning — "unset, use the default" — rather than becoming a one-step turn.
    _caller_cap = max_steps if isinstance(max_steps, int) and max_steps > 0 else None
    # 0 = NO step cap (Settings → Agent limits, or AIFORGE_CHAT_SAFETY_CAP=0).
    # The turn then ends the way a turn normally ends: the agent finishes, a
    # stall guard fires, the wall-clock deadline hits, or the user hits Stop.
    #
    # …but Stop is gated on a session id (see the cancel check below), so an
    # UNATTENDED run — the jobs scheduler, the analysis fan-out, the subtask
    # runners, text_doer — has no brake at all once the cap is off. "No limits"
    # is a promise to someone sitting in front of a chat; those runs keep a cap.
    _unattended = _cap_base <= 0 and _caller_cap is None and session_id is None
    if _unattended:
        _cap_base = _unattended_cap()
    safety = _caller_cap or _cap_base
    _capped = safety > 0
    return _cap_base, _caller_cap, _unattended, safety, _capped


def _build_loop_state(messages, cwd, role, max_steps, complete_fn,
                      session_id, mode, scope_globs, builder, strict_finish):
    """Assemble everything the ReAct loop needs (native detection, mode/read-only
    flags, scope allowlist, budget-capped convo, cap/deadline/extension budgets,
    the request meter and every per-turn counter) into one st namespace."""
    complete_fn, _native_on = _resolve_complete_fn(complete_fn, role)
    from aiforge_core.runtime import chat_approve, chat_cancel, chat_interject
    from aiforge_core.runtime.tools import tool_policy
    chat_cancel.set_active(session_id)
    _mode = (mode or "act").lower()
    plan_mode = _mode == "plan"
    analyze_mode = _mode == "analyze"
    # Both plan and analyze are READ-ONLY (same tool gate); they differ only in
    # the banner/output intent — plan produces a change-PLAN, analyze produces
    # FINDINGS. Used by the analysis fan-out's explore agents.
    readonly_mode = plan_mode or analyze_mode
    # Scope allowlist (autonomous Doer). When the caller passes globs, a
    # mutating file tool whose target path falls outside them is rejected
    # BEFORE it runs — the FunctionNode text Doer can't carry the native
    # scope_guard before_tool_callback, so this is its equivalent jail.
    # Empty/None = no restriction (back-compat; the chat UI passes nothing).
    _scope_globs = [g for g in (scope_globs or [])
                    if isinstance(g, str) and g]

    import collections
    # Caller-supplied max_steps (chat Quick mode, tests) is a DELIBERATE small
    # budget — honour it exactly and never auto-extend it. Only the
    # operator-level cap (Settings → env → default) is extendable, and only on
    # an interactive turn (see _ext_budget below).
    _cap_base, _caller_cap, _unattended, safety, _capped = _compute_caps(
        max_steps, session_id)
    # Wall-clock turn backstop. The 2000-step cap is not a real stopping
    # point on a slow local model — 2000 steps × seconds-to-minutes each is
    # effectively "forever" from the user's chair. This deadline bounds the
    # WHOLE turn regardless of step count, so a wandering or churning agent
    # (evades the exact-repeat stall guards below by varying its args) can't
    # run for hours. Generous default (1h) so it's a backstop, not a normal
    # limit; 0 disables. Set in Settings → Agent limits (or
    # AIFORGE_CHAT_TURN_DEADLINE_S).
    _turn_budget_s = _turn_deadline_s()
    _turn_deadline = (time.monotonic() + _turn_budget_s) if _turn_budget_s > 0 else None

    # Latest user message drives mentions (#4) + skill triggers (#6) +
    # memory recall. In simple/plan mode the API augments the last user turn
    # with an "[Interpreted request …]" enhancer block; key off the user's RAW
    # words (split that marker off) so recall/skills/mentions aren't diluted by
    # the boilerplate + restatement.
    convo, _bundle, _asks, _dropped_playbooks = _build_convo(
        messages, cwd, role, readonly_mode=readonly_mode,
        plan_mode=plan_mode, analyze_mode=analyze_mode, builder=builder,
        strict_finish=strict_finish, session_id=session_id)

    # OrderedDict, not dict: the prune below needs least-recently-SEEN order,
    # which only move_to_end can maintain (see its call site).
    action_counts: "collections.OrderedDict[str, int]" = collections.OrderedDict()
    recent_outputs: collections.deque = collections.deque(maxlen=_OUTPUT_REPEAT)
    condensed_notified = False
    continue_nudges = 0   # consecutive "narrated but didn't act" re-prompts
    stuck_recoveries = 0  # progress-recap nudges spent recovering a repeated step
    read_sigs_seen: set = set()   # read tool+args already executed this run
    # SEPARATE from read_sigs_seen, and never cleared: read_sigs_seen exists to
    # short-circuit a duplicate read while its RESULT is still in the window, so
    # a condense (which drops those results) must clear it. Progress is a
    # different question — "has this turn learned anything it did not know" —
    # and re-reading a file after a condense is not new knowledge. Conflating
    # the two handed a pure re-read loop the whole extension budget.
    # Bounded like action_counts, and for the same reason: an uncapped turn has
    # no step ceiling to hold it down. Losing the oldest entries only means an
    # ancient read can count as "new knowledge" a second time — the failure
    # direction that grants an extension, never one that hides a runaway.
    read_sigs_ever: "collections.OrderedDict[str, bool]" = collections.OrderedDict()
    _long_chain_help = _stuck_recovery_max() > 0   # 0 → full legacy behaviour

    # Mid-run steering (simple mode): let the user type WHILE the agent works —
    # each message is folded into the conversation as a live instruction the next
    # step must honour (parity with the pipeline's steering).
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_interject as _ci
            _ci.set_steerable(session_id, True)
        except Exception:  # noqa: BLE001
            pass


    # ── Budget extensions ────────────────────────────────────────────────
    # The step cap and the turn deadline are RUNAWAY guards, not task budgets.
    # A turn that is still producing NEW work earns another budget instead of
    # being killed with its work thrown away; a turn that is only spinning is
    # stopped exactly as before. Caller-set max_steps is never extended.
    # Only an INTERACTIVE turn extends. The unattended callers (text_doer,
    # parallel_subtasks, the analysis fan-out) pass no max_steps and have no
    # one watching — tripling their ceiling is spend nobody asked for. They
    # keep the old hard stop.
    # _extension_budget also bounds the PRODUCT (cap × (1+extensions), in steps
    # AND in wall clock) — each settings field validates in isolation, so the
    # multiplication is where an innocent-looking pair becomes a multi-day turn.
    _ext_budget = (_extension_budget(_cap_base, _turn_budget_s)
                   if (_caller_cap is None and session_id is not None) else 0)
    _extensions_used = 0
    _granted_at_step = -1     # step whose extension is already paid for
    # Progress is NEW WORK, not novel tool arguments: an agent that varies its
    # args mints a new action signature every step — that is the very churn the
    # deadline exists to stop, so counting distinct actions would hand every
    # extension to the runaway. Count what actually changes the world or the
    # agent's knowledge: file edits that landed, and reads of something not
    # read before.
    _reads_new = 0
    # Request meter: READ ONLY here. The turn boundary belongs to the route
    # (chat.py `_produce`), which owns the whole turn — including the enhancer
    # and classifier calls that happen before this loop, and team mode, which
    # never enters this function at all.
    try:
        from aiforge_core.llm import call_meter as _meter
    except Exception:  # noqa: BLE001 — metering must never break a turn
        _meter = None
    _builder_nudged = False
    _builder_finalized = False
    _builder_final_tries = 0
    _multiask_checked = False   # one-time FINAL completeness gate (multi-ask)
    # Loop-engineering state (verify→fix on FINAL + progress gating). Only a
    # work-producing "act" run that actually EDITED files, with a real test
    # suite present, gets the verify gate — a Q&A turn (0 edits) is untouched.
    _edits_made = 0
    _verify_rounds = 0
    _verify_prev_fails = None   # last measured failure count (progress signal)
    _verify_stalls = 0
    # Claim-vs-reality guard: baseline the working tree ONCE so a final answer
    # claiming edits can be cross-checked against a real on-disk change (any
    # tool, not just the counted ones). "" = non-git workspace / no signal.
    # Skip the git call entirely when the guard is off.
    _wt_fp0 = _worktree_fingerprint(cwd) if _edit_claim_guard_enabled() else ""
    _edit_claim_nudges = 0
    # Progress mark at the last extension: (new reads, landed edits, worktree
    # fingerprint). Seeded with the turn's OWN baseline so an unchanged tree
    # does not read as a change on the first check.
    _progress_mark = (0, 0, _wt_fp0)



    st = types.SimpleNamespace(
        convo=convo, safety=safety, turn_deadline=_turn_deadline,
        condensed_notified=condensed_notified, continue_nudges=continue_nudges,
        stuck_recoveries=stuck_recoveries, extensions_used=_extensions_used,
        granted_at_step=_granted_at_step, reads_new=_reads_new,
        edits_made=_edits_made, progress_mark=_progress_mark,
        builder_nudged=_builder_nudged, builder_finalized=_builder_finalized,
        builder_final_tries=_builder_final_tries, multiask_checked=_multiask_checked,
        verify_rounds=_verify_rounds, verify_prev_fails=_verify_prev_fails,
        verify_stalls=_verify_stalls, edit_claim_nudges=_edit_claim_nudges,
        action_counts=action_counts, recent_outputs=recent_outputs,
        read_sigs_seen=read_sigs_seen, read_sigs_ever=read_sigs_ever,
        cap_base=_cap_base, ext_budget=_ext_budget, wt_fp0=_wt_fp0,
        turn_budget_s=_turn_budget_s, long_chain_help=_long_chain_help, cwd=cwd,
        capped=_capped, caller_cap=_caller_cap, unattended=_unattended,
        role=role, complete_fn=complete_fn, session_id=session_id,
        builder=builder, strict_finish=strict_finish, plan_mode=plan_mode,
        analyze_mode=analyze_mode, readonly_mode=readonly_mode,
        scope_globs=_scope_globs, asks=_asks, bundle=_bundle, meter=_meter,
        dropped_playbooks=_dropped_playbooks, native_on=_native_on)
    return st


def _emit_loop_prelude(st):
    """Emit the one-time turn prelude events: a dropped-playbook warning, the
    multi-ask subtasks dock, and the native-tool-calling banner."""
    if st.dropped_playbooks:
        yield {"type": "thought", "role": "system",
               "text": "⚠ context window too small — dropped the "
                       + " + ".join(st.dropped_playbooks) + " block(s): matched "
                       "workflows/skills may NOT be followed this turn. Load "
                       "the model at a larger context window to fix this."}
    if st.asks:
        yield {"type": "subtasks", "items": [
            # `goal` is the field name every other producer uses and the one
            # the UI's Tasks panel reads. This path emitted `title` alone, so
            # expanding that panel on a split-asks turn threw "Cannot read
            # properties of undefined (reading 'length')" and the view died.
            # Both keys go out: `title` is kept for anything already reading it.
            {"slug": f"part-{i + 1}", "goal": a, "title": a, "status": "pending"}
            for i, a in enumerate(st.asks)]}
    # One-time visibility: confirm native tool-calling is driving this run (every
    # tool call goes through native OpenAI function-calling, not the text
    # ACTION/ARGS_JSON protocol). Opt out of the banner: AIFORGE_CHAT_NATIVE_BANNER=0.
    if st.native_on and os.environ.get("AIFORGE_CHAT_NATIVE_BANNER", "1") not in ("0", "false"):
        try:
            from ._tools._schemas import NATIVE_TOOL_SCHEMAS
            _ntools = len(NATIVE_TOOL_SCHEMAS)
        except Exception:  # noqa: BLE001
            _ntools = 0
        yield {"type": "thought", "role": "system",
               "text": f"🔌 native tool-calling active ({_ntools} tools)"}


def _step_prologue(st, n, _cwd, role, complete_fn, session_id, builder):
    """Per-step prologue up to a parsed reply: cap/deadline/cancel guards, builder
    nudge, steering drain, condense+usage, the model completion, and the stuck-
    output guard. Returns ``(out, signal)`` with signal 'return'/'continue'/None."""
    from aiforge_core.runtime import chat_cancel
    _sig = yield from _step_cap_guard(st, n)
    if _sig == "return":
        return None, "return"
    if session_id is not None and chat_cancel.is_cancelled(session_id):
        yield {"type": "error", "text": "stopped by user"}
        yield {"type": "done"}
        return None, "return"
    _builder_nudge(st, builder, n)
    _sig = yield from _deadline_guard(st, n)
    if _sig == "return":
        return None, "return"
    yield from _drain_steering(st, session_id)
    yield from _condense_and_report(st, role, complete_fn, session_id, st.meter)
    out = yield from _run_completion(st, role, complete_fn, session_id, st.meter)
    if out is _RETRY_STOP:
        return None, "return"
    _sig = yield from _stuck_output_guard(st, out)
    if _sig == "return":
        return None, "return"
    if _sig == "continue":
        return None, "continue"
    return out, None


def _run_action_path(st, step, n, cwd, session_id):
    """The action path for a tool step: stall guard, plan/approval/hook/scope
    gates, tool dispatch and post-tool bookkeeping. Returns return/continue/None."""
    # action
    name = step["tool"]
    # Coerce to a dict: a model can emit `ARGS_JSON: null` (or a JSON scalar)
    # which parses to None/non-dict; every tool does `args.get(...)` and would
    # crash. An empty dict lets the tool return its own instructive error.
    args = step["args"] if isinstance(step["args"], dict) else {}
    sig = name + "|" + json.dumps(args, sort_keys=True, default=str)
    _sig = yield from _action_stall_guard(st, name, args, sig, st.long_chain_help)
    if _sig == "return":
        return "return"
    if _sig == "continue":
        return "continue"
    if step.get("thought"):
        yield {"type": "thought", "text": step["thought"]}
    _sig = yield from _pre_dispatch_gates(st, name, args, st.readonly_mode,
                                          st.analyze_mode)
    if _sig == "continue":
        return "continue"
    _sig = yield from _approval_gate(name, args, cwd, session_id, st.convo)
    if _sig == "return":
        return "return"
    if _sig == "continue":
        return "continue"
    _hb = yield from _pre_tool_checks(st, name, args, cwd, st.scope_globs)
    if _hb == "continue":
        return "continue"
    result = yield from _dispatch_tool(name, args, cwd, n, _hb)
    yield from _post_tool(st, name, args, result, cwd, sig, n,
                          st.long_chain_help, st.bundle)
    return None


def _dispatch_step(st, out, n, cwd, role, _complete_fn, session_id, builder,
                   strict_finish):
    """Process one parsed reply: FINAL / ask / continue handling, then the action
    path (stall guard, plan/approval/hook/scope gates, tool dispatch, post-tool
    bookkeeping). Returns 'return'/'continue'/None."""
    st.convo.append({"role": "assistant", "content": out})
    step = _parse(out)
    if step["kind"] == "final":
        _sig = yield from _handle_final(
            st, step, builder, strict_finish, st.plan_mode, st.readonly_mode,
            cwd, st.asks, st.wt_fp0)
        if _sig == "return":
            return "return"
        if _sig == "continue":
            return "continue"
    if step["kind"] == "ask":
        # Agent is asking the user a question — show it + wait for the next
        # message (which answers it). awaiting_input flags the UI.
        yield {"type": "message", "awaiting_input": True, "text": step["text"]}
        yield {"type": "done"}
        return "return"
    if step["kind"] == "continue":
        _sig = yield from _handle_continue_step(st, step, builder, cwd)
        if _sig == "return":
            return "return"
        if _sig == "continue":
            return "continue"
    st.continue_nudges = 0   # a real action resets the narration guard
    return (yield from _run_action_path(st, step, n, cwd, session_id))


def run_chat_agent(
    messages: list[dict], *,
    cwd: str,
    role: str = "doer",
    max_steps: int | None = None,   # kept for callers/tests; None = no cap
    complete_fn: Callable[..., str] | None = None,
    session_id: int | None = None,
    mode: str = "act",              # "act" = full tools; "plan" = read-only
    scope_globs: list[str] | None = None,  # autonomous Doer scope allowlist
    builder: str | None = None,     # job|skill|workflow|rule — task charter
    strict_finish: bool = False,    # work-producing run (doer): an IMPLICIT
    #                                 bare-prose final is premature narration →
    #                                 nudge to act, don't quit with no work done
) -> Iterator[dict]:
    """Drive the ReAct loop until the agent finishes or a stuck loop is
    detected (NOT a step count). Yields SSE-ready event dicts:

    ``{"type": "thought", "text"}`` · ``{"type": "tool", "name", "args",
    "result"}`` · ``{"type": "message", "text"}`` (final) ·
    ``{"type": "approval", ...}`` (ask-policy gate) ·
    ``{"type": "error", "text"}`` · ``{"type": "done"}``.
    """
    from aiforge_core.runtime import chat_cancel
    st = _build_loop_state(
        messages, cwd, role, max_steps, complete_fn, session_id, mode,
        scope_globs, builder, strict_finish)
    # _build_loop_state RESOLVES the completion fn (injects native tool-calling
    # when the caller passed none, as chat does) into st.complete_fn. The loop
    # below still threads a `complete_fn` local into _step_prologue/_run_completion
    # — rebind it to the resolved one, or native chat calls None(role, convo)
    # ("'NoneType' object is not callable" → the "model didn't respond" retry
    # loop on a model that answered fine). Regression from the run_chat_agent
    # decomposition: the resolve moved into the helper but the local kept the
    # caller's original None.
    complete_fn = st.complete_fn
    n = 0
    yield from _emit_loop_prelude(st)
    while True:
        n += 1
        out, _sig = yield from _step_prologue(
            st, n, cwd, role, complete_fn, session_id, builder)
        if _sig == "return":
            return
        if _sig == "continue":
            continue
        _sig = yield from _dispatch_step(
            st, out, n, cwd, role, complete_fn, session_id, builder, strict_finish)
        if _sig == "return":
            return
        if _sig == "continue":
            continue