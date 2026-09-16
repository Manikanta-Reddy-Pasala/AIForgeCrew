"""The per-turn producer: builds the event stream and runs it in the
background, with its turn-setup helpers (logger, request meter,
stale-notes notice)."""
from __future__ import annotations

import json
import os

from ._core import (
    _PRODUCE_SEM,
    _af_log,
)
from ._prep import (
    _auto_checkpoint,
    _with_resume,
)
from ._routing import (
    _decide_chat_route,
    _maybe_downgrade_team,
    _plan_mode_route,
    _rule_capture_pass,
    _should_skip_enhance,
)
from ._stages import (
    _commit_simple_baseline,
    _dispatch_agent_route,
    _early_route_events,
    _enhance_prompt,
    _fold_enriched_history,
    _post_run_events,
    _prelude_notices,
    _single_agent_events,
)
from ._turn_events import (
    _drive_produce_stream,
    _finalize_produce_turn,
    _TurnResetContext,
)


def _setup_chat_logger():
    """The shared "chat" observability logger + its emit fn, so the Logs "chat"
    tab tails one file. Returns ``(clog, emit)`` — ``(None, None)`` if the
    observability module is unavailable. (Don't stash a per-session ticket on the
    process-wide singleton — concurrent sessions would clobber it; the caller
    stamps ``session`` per emit.)"""
    try:
        from aiforge_core.observability.logging import emit, get_logger
        return get_logger("chat"), emit
    except Exception:  # noqa: BLE001
        return None, None


def _bind_turn_meter(session_id):
    """Bind THE per-turn request-meter boundary here (not inside the ReAct loop):
    the enhancer / team-downgrade classifier / capture probes below are requests
    THIS message caused, and team mode never enters run_chat_agent at all — a
    reset in the loop left its per-turn number cumulative for the session.
    Returns ``(meter, meter_token)``; metering must never break a turn."""
    try:
        from aiforge_core.llm import call_meter as _meter
        return _meter, _meter.bind_turn(_meter.turn_reset(session_id))
    except Exception:  # noqa: BLE001
        return None, None


def _note_staleness_notice(cwd):
    """Staleness auto-curation: a session bound to a jira/confluence context
    folder re-verifies that note when it crosses AIFORGE_NOTE_STALE_HOURS. The
    pre-check is cheap + network-free; the curation re-fetches the source so it
    is HARD time-boxed — a dead Jira must never stall the turn. Yields a curator
    thought ONLY when something actually drifted. FAILS OPEN."""
    try:
        from aiforge_core.runtime import note_curator as _nc
        _stale_note = _nc.stale_note_path(cwd)
        if _stale_note:
            import concurrent.futures as _ncf
            _cres = None
            _nex = _ncf.ThreadPoolExecutor(max_workers=1)
            try:
                _nbudget = float(os.environ.get(
                    "AIFORGE_NOTE_CURATE_BUDGET_S", "10"))
                _cres = _nex.submit(_nc.curate_note,
                                    _stale_note).result(timeout=_nbudget)
            except Exception as _nexc:  # noqa: BLE001 — timeout/any → skip
                _af_log.debug("note curation timed out/failed: %s", _nexc)
            finally:
                _nex.shutdown(wait=False)
            # Visible only when something actually drifted — a silent
            # freshness bump shouldn't add chat noise.
            if _cres and _cres.get("ok") and _cres.get("changes"):
                yield {"type": "thought", "role": "curator",
                       "text": ("Auto-curated stale note "
                                f"{os.path.basename(_stale_note)}: "
                                + "; ".join(_cres["changes"]))}
    except Exception as _nexc2:  # noqa: BLE001 — must never break a turn
        _af_log.debug("note staleness pass skipped: %s", _nexc2)


def _events(pc):
    pctx0 = {"done": False}
    yield from _early_route_events(pc._cmd_help_text, pc.body, pc.history, pc.cwd, pc.role,
                                   pc.session_id, pctx0)
    if pctx0["done"]:
        return
    yield from _prelude_notices(pc._resume_brief, pc._cmd_expanded)
    # Staleness auto-curation: a session bound to a jira/confluence
    # context folder (cwd = work/<kind>/<key>) re-verifies that context's
    # note when its updated_at crossed AIFORGE_NOTE_STALE_HOURS. The
    # pre-check is cheap and network-free; the actual curation re-fetches
    # the source, so it's HARD time-boxed (like the rule_capture pass
    # below) — a dead Jira must never stall the chat turn. FAILS OPEN.
    yield from _note_staleness_notice(pc.cwd)
    # Rule / Memory / Feedback capture (deterministic, always-on) — runs
    # BEFORE any agent, independent of the agent's model, so a directive /
    # fact / correction stated in passing is captured + applied. FAILS OPEN:
    # any error here is swallowed and the normal run proceeds.
    pctx = {"done": False}
    yield from _rule_capture_pass(pc.prompt, pc.cwd, pc.session_id, pctx)
    if pctx["done"]:
        return
    # Team mode → full ADK agent flow (planner→…→learner) for complex
    # builds. Simple mode → single conversational agent for quick work.
    # Parallel team mode (AIFORGE_PARALLEL_SUBTASKS=1) → decompose then run
    # subtasks CONCURRENTLY in isolated worktrees with live status.
    from aiforge_core.runtime import parallel_subtasks as _pp
    # AUTO-ESCALATE: simple/plan modes on a multi-file BUILD request route
    # through the parallel pipeline — a single ReAct agent stalls on large
    # builds (one huge-context call, no decomposition). Gated + heuristic so
    # chit-chat / small edits still use the fast single-agent path.
    # ── TASK-TYPE ROUTING — see aiforge_core.runtime.chat_router ──────────
    # The heavy decision (which path handles this request) is a PURE function
    # there; here we only gather its side-effecting inputs and dispatch:
    #   • _psub_on   parallel capability (raw — escalation can fire off-team);
    #   • _greenfield  is this a fresh/empty tree?;
    #   • _fresh     NOT a follow-up (only fresh turns pay the LLM classify);
    #   • _cat       the LLM class (chat|tracker|doc_analysis|code_build|
    #                code_edit) or None → chat_router falls back to regex;
    #   • _team_approvals  Pipeline-approvals ON → force the gated sequential
    #                pipeline (the parallel path can't gate — J).
    _rd = _decide_chat_route(_pp, pc.prompt, pc.agent_mode, pc.team,
                             pc._parallel_team, pc.cwd, pc.history)
    _doc_task = _rd.doc_task
    _is_build_task = _rd.is_build_task
    _build_escalate = _rd.build_escalate
    _route_pipeline = _rd.route_pipeline
    rctx = {"done": False}
    yield from _dispatch_agent_route(
        _rd, _pp, pc.prompt, pc.cwd, pc.session_id, pc.history, lambda t: _with_resume(pc, t), pc._path,
        pc._turn_t0, pc.team, pc._resume_brief, rctx)
    if rctx["done"]:
        return
    # SIMPLE and PLAN modes — the Enhancer is MANDATORY on the FIRST turn
    # of a session (fresh context, referents to resolve, no memory pulled
    # yet). On a FOLLOW-UP, re-running the enhancer (an LLM round-trip
    # that also fires the memory recall inside `_enhance`) on every single
    # message is wasted latency for the common case ("fix that", "add a
    # test") — so reuse the same cheap classify already used to
    # auto-downgrade team turns (turn_router.classify) and skip the
    # enhancer when this follow-up is small. Any classify failure (or the
    # first turn, or a build-escalate spec already in flight) keeps the
    # enhancer mandatory — safe default, never silently under-enhance.
    _skip_enhance = _should_skip_enhance(pc._auto_downgraded, _route_pipeline,
                                         _is_build_task, pc.history, pc.prompt)
    if pc._auto_downgraded:
        yield {"type": "thought", "role": "router",
               "text": "Small follow-up — handling directly (skipped the "
                       "full pipeline for speed)."}
    if not _skip_enhance:
        yield {"type": "thought", "role": "enhancer",
               "text": "Enhancing request + gathering context…"}
    _enriched = _enhance_prompt(_pp, pc.prompt, pc.history, pc.cwd, _skip_enhance)
    _enriched_history = _fold_enriched_history(
        pc.history, _enriched, pc._resume_brief, pc.prompt, _doc_task)
    if pc.agent_mode == "plan":
        yield from _plan_mode_route(_pp, _enriched, _enriched_history, pc.cwd,
                                    pc.role, pc.session_id, pc.body.quick)
        return
    # Baseline commit so we can show a Changes diff after the single-agent run
    # (simple mode edits the working tree; the pipeline shows its own Changes).
    # A fresh chat workspace is NOT a git repo — the old rev-parse/empty-tree
    # dance then left _simple_sha unusable (git diff needs a real repo), so the
    # Changes view silently vanished. _ensure_git_workspace git-inits + makes a
    # committed baseline (no-op when cwd is already a repo, e.g. a pinned user
    # project), so HEAD is ALWAYS a valid baseline to diff the run against.
    # CRITICAL: commit the CURRENT working-tree state into the baseline so
    # this turn's Changes diff + the "did it write source?" gate reflect ONLY
    # what THIS turn does. A reused chat/ticket workspace (e.g. session-1)
    # carries a previous task's uncommitted files; without this snapshot,
    # `git status` reports THEM, so a no-code Jira/Q&A turn wrongly triggers
    # the build/integration pipeline on stale files and the Changes view
    # shows the previous ticket's edits.
    _simple_sha, _skip_worktree = _commit_simple_baseline(pc.cwd)
    _single_mode = "analyze" if _doc_task and pc.agent_mode != "plan" else pc.agent_mode
    awaiting_ctx = {"awaiting": False}
    yield from _single_agent_events(_enriched_history, pc.cwd, pc.role, pc.session_id,
                                    _single_mode, pc.body.quick, awaiting_ctx)
    # A turn that ended AWAITING user input (a REJECT/ASK) must NOT fall into
    # the post-run integration build: on a turn with an earlier APPLIED edit,
    # _turn_wrote_source() is True and the build fires AFTER the reject, holds
    # the is_running slot and 409-blocks the user's next (resume) message.
    if awaiting_ctx["awaiting"]:
        return
    yield from _post_run_events(pc.prompt, pc.cwd, pc.agent_mode, _simple_sha)


def _produce(pc):
    from aiforge_core.runtime import chat_approve as _chat_approve
    from aiforge_core.runtime import parallel_subtasks as _psub
    _PRODUCE_SEM.acquire()   # bounded — block until a producer slot frees
    # Bind this producer thread to the session so LLM tracing (Langfuse
    # sessions/scores) tags every generation with the run it belongs to.
    # Covers ALL modes here (simple/plan run inline in this thread; team's
    # _drive re-sets the env in its own thread). Env for cross-thread /
    # subprocess reach; contextvar for concurrency-correct in-thread reads.
    os.environ["AIFORGE_CURRENT_SESSION"] = str(pc.session_id)
    # Hold the machine awake for the WHOLE turn, every mode. Team runs and
    # jobs already do it for themselves; doing it here as well means the
    # answer to "will my work survive me locking the screen" is yes for
    # anything the user can start, not just the two slowest paths. The
    # refcount makes the overlap free — nested holders share one child.
    from aiforge_core.runtime.keep_awake import acquire as _awake_acquire
    from aiforge_core.runtime.keep_awake import release as _awake_release
    _awake_acquire()
    from aiforge_core.runtime import request_context as _reqctx
    _sess_token = _reqctx.set_session_id(pc.session_id)
    # THE turn boundary for the request meter. Here, not inside the ReAct
    # loop: the enhancer / team-downgrade classifier / capture probes below
    # are requests this message caused, and resetting after them erased
    # them from the count. Team mode never enters run_chat_agent at all, so
    # a reset in the loop left its per-turn number cumulative for the whole
    # session — a lifetime total presented as one message's cost.
    _meter, _meter_token = _bind_turn_meter(pc.session_id)
    # Bind the repo root to the turn's cwd so the codegraph gate (which some
    # Doer-side call sites resolve via request_context.get_repo_root() with
    # NO cwd) sees the SAME repo the tools run against. Without this, simple
    # chat left the repo root unset and those sites fell back to "." (the
    # AIForge process dir), so codegraph was mis-gated off the wrong folder.
    _repo_token = _reqctx.set_repo_root(pc.cwd)
    # Auto-route classify + its dependents, run HERE (already off the
    # response-open path — see the note where `team`/`_parallel_team`
    # were declared above) rather than in the synchronous request
    # handler, so a slow/unreachable classify LLM never delays the
    # StreamingResponse itself.
    pc.team, pc._auto_downgraded = _maybe_downgrade_team(
        pc.team, pc.prompt, pc.history, pc.cwd, pc.session_id)
    pc._parallel_team = pc.team and _psub.enabled()
    # Review-edits gate: OFF by default — file writes/patches auto-apply,
    # no per-edit Approve/Reject prompt (the operator asked for no file-
    # permission prompts). Re-enable per-request via body.review_edits, or
    # globally with AIFORGE_CHAT_REVIEW_EDITS=1. Team/parallel mode never
    # holds edits regardless (the full pipeline runs unattended).
    _review_env = os.environ.get(
        "AIFORGE_CHAT_REVIEW_EDITS", "0") in ("1", "true", "yes", "on")
    _chat_approve.set_review_edits(
        pc.session_id, (bool(pc.body.review_edits) or _review_env) and not pc.team)
    # Record the EFFECTIVE run mode (after any team→simple downgrade) so the
    # tool gate can honor the per-mode approval Settings toggle.
    if pc.team:
        _eff_mode = "team"
    elif pc.agent_mode == "plan":
        _eff_mode = "plan"
    else:
        _eff_mode = "simple"
    _chat_approve.set_mode(pc.session_id, _eff_mode)
    steps: list[dict] = []
    final_text = ""
    awaiting = False   # turn ended with a question / pause, not an outcome
    _subtasks: list[dict] = []   # live subtask panel state, persisted so it
    #                              survives a navigate-away / reload
    # Mirror chat activity into the observability NDJSON so the Logs page
    # shows live runs (the page tails orchestrator-<role>.ndjson).
    _clog, emit = _setup_chat_logger()
    _auto_checkpoint(pc)   # snapshot first (off the response-open path)
    st = {"final_text": final_text, "awaiting": awaiting, "subtasks": _subtasks}
    try:
        _drive_produce_stream(lambda: _events(pc), st, steps, pc.run, pc.session_id,
                              pc._turn_t0, pc._turn_mode, _clog, emit)
    finally:
        _finalize_produce_turn(
            pc.session_id, pc.cwd, pc.prompt, st["final_text"], steps, st["awaiting"],
            pc.team, pc._path, pc._turn_mode, pc._turn_t0,
            _TurnResetContext(_meter, _meter_token, _reqctx, _sess_token, _repo_token),
            pc.run, _awake_release)


def _stream(pc):
    from aiforge_core.runtime import chat_runs
    # Tail the live run as SSE. A client disconnect only closes this
    # subscriber — the producer thread keeps running.
    q = pc.run.subscribe()
    for ev in chat_runs.iter_subscription(pc.run, q):
        yield f"data: {json.dumps(ev)}\n\n"
