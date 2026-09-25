"""System-prompt blocks: recall, session and learning context, and the
priority blocks that must survive trimming."""
from __future__ import annotations

import os

from .._context import (
    _chat_session_recall,
    _ctx_on,
)
from .._registry import (
    _ANALYZE_BANNER,
    _PLAN_BANNER,
)
from .._tools import (
    _chat_repo_key,
)


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
            "MEMORY: the project brief and anything already recalled for this "
            "turn are above. That context is already gathered. Do not call "
            "memory_lookup for it. Look something up only when it is not "
            "already in this prompt.")

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
    # A short remark does not search memory. Images and the ledger, appended
    # just below, still go on.
    _skip_recall = False
    try:
        from aiforge_core.runtime.chat_router import plain_chat
        _skip_recall = plain_chat(last_user or "")
    except Exception:  # noqa: BLE001
        _skip_recall = False
    if not _skip_recall:
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
    # A short remark skips the repo walk, the skill match and the memory
    # query — those run before the model speaks. Images, the session ledger
    # and workflows stay: a screenshot plus "what's this?", or "ok, commit",
    # still needs them.
    _plain = False
    try:
        from aiforge_core.runtime.chat_router import plain_chat
        _plain = plain_chat(last_user or "")
    except Exception:  # noqa: BLE001 — a classifier miss still builds context
        _plain = False
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
    _recall_wanted = (not _plain) and (_proactive == "full" or _is_init)

    def _ctx(block: str) -> bool:
        if _plain and block in ("recall", "skills", "repomap", "summary"):
            return False
        return _ctx_on(block) and (block != "recall" or _recall_wanted)

    _bundle = _cb.build_bundle(
        cwd, last_user, cave=cave, ctx_on=_ctx, session_id=session_id,
        want_rules=False, want_prefs=False,
        want_repo_map=not _plain, want_summary=not _plain)
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
                     "plan_progress with slug part-N and status running, and "
                     "when it is DONE call it again with status done — the "
                     "user watches this live."
                   + "\n\n" + sys_msg)
    if prefs:                       # standing user preferences — always applied
        sys_msg = prefs + "\n\n" + sys_msg
    if rules:                       # user rule book first — highest priority
        sys_msg = rules + "\n\n" + sys_msg
    if analyze_mode:                # read-only ANALYSIS (findings, not a plan)
        sys_msg = _ANALYZE_BANNER + "\n\n" + sys_msg
    elif plan_mode:                 # plan banner second — constrains this turn
        # Native plan mode already sent the short read-and-plan rules.
        if "You are read-only this turn" not in sys_msg:
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
