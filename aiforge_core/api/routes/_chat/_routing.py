"""Choosing how a turn runs: pipeline, document task, plan mode, and capture
passes."""
from __future__ import annotations

import os

from ._core import (
    _af_log,
)


def _quick_step_cap(quick: bool) -> int | None:
    """Hard step cap for a quick turn, or None for the normal open loop.

    The chat agent normally runs until it decides it is done (a stuck-loop
    detector bounds it, not a step count) — right for real work, and the reason
    a one-line ask can still cost minutes of exploration. Quick mode trades that
    thoroughness for latency: the agent gets a handful of steps, and if it needs
    more the user can simply ask again without the toggle.
    """
    import os as _os

    if not quick:
        return None
    try:
        return max(1, int(_os.environ.get("AIFORGE_CHAT_QUICK_STEPS", "6")))
    except ValueError:
        return 6


def _maybe_downgrade_team(team, prompt, history, cwd, session_id):
    """Auto-route a small team follow-up down to simple mode. Run HERE (off the
    response-open path) so a slow/unreachable classify LLM never delays the
    StreamingResponse. Returns ``(team, auto_downgraded)``; routing must never
    block a turn."""
    if not team:
        return team, False
    try:
        from aiforge_core.runtime import turn_router as _tr
        if _tr.should_downgrade_team(prompt, history, cwd):
            _af_log.info("chat: team turn auto-downgraded to simple "
                         "(small follow-up) session=%s", session_id)
            return False, True
    except Exception as exc:  # noqa: BLE001
        _af_log.debug("turn_router skipped: %s", exc)
    return team, False


def _pipeline_route(_pp, prompt, cwd, session_id, history, _with_resume, _path,
                    _turn_t0, _pctx):
    """The 3-agent orchestrator route (enhancer → architect → planner). A spec
    that splits into >=2 files runs the parallel team; otherwise it still writes
    SPEC.md (every pipeline-routed run tracks against one) and falls to best-of-N
    (opt-in) or the sequential team. Enhancer recall is SCOPED to this session's
    repo so an unrelated task can't bleed into the build spec. Sets
    ``pctx["done"]`` on the parallel / best-of-N returns."""
    # Orchestrator (layer 1) = 3 agents: enhancer → architect → planner.
    # SCOPE the enhancer's memory recall to THIS session's repo — without
    # a repo, unified_query runs its repo-agnostic sources (prior chat
    # sessions + global vector) and an UNRELATED task bleeds into the
    # build spec (a "mathx" build decomposed into game/storage). The
    # contamination guard in unified_query only fires with a repo set.
    from aiforge_core.runtime.chat_agent import _chat_repo_key as _crk
    _pl_repo = _crk(cwd)
    # Same token budget as before (the chat path passes its own cap). Stop
    # aborts the call; without session_id the enhancer waited out its timeout.
    _spec = _pp._enhance(prompt, history=history, cwd=cwd, repo=_pl_repo,
                         session_id=session_id)  # 1. clean spec
    from aiforge_core.runtime import chat_cancel as _cc
    if session_id is not None and _cc.is_cancelled(session_id):
        yield {"type": "error", "text": "stopped by user"}
        yield {"type": "done"}
        _pctx["done"] = True
        return
    _pctx["spec"] = _spec   # a single-task fall-through reuses it, not a 2nd enhance
    _files = _pp._architect(_spec, cwd=cwd)  # 2. design file structure
    _subs = _pp._plan_files(_files) if len(_files) >= 2 \
        else _pp._decompose(_spec)          # 3. split (per file, or plan)
    if len(_subs) >= 2:
        _path["parallel"] = True
        yield from _pp.stream_parallel_team(_with_resume(_spec), cwd=cwd,
                                            subtasks=_subs,
                                            enhanced=True, session_id=session_id)
        # stream_parallel_team emits no terminal `done`; synthesize one
        # so a UI waiting on `done` doesn't hang (exactly one — the
        # exception path in _gen only fires on error). AND mark the turn
        # done: without it the dispatcher fell through and ran the same
        # request AGAIN (single agent in simple mode, the whole sequential
        # team in team mode) after the UI had been told it was finished.
        yield {"type": "done"}
        _pctx["done"] = True
        return
    # Couldn't split into ≥2 distinct files → it's really ONE task.
    # STILL write SPEC.md (user requirement: every pipeline-routed run
    # tracks against a spec): only stream_parallel_team used to write
    # it, so the <2-subtask fallbacks (best-of-N / sequential / single
    # agent) ran spec-less — 'sometimes there is no SPEC.md'.
    try:
        _spec_doc = _pp._render_spec_md(_spec, _subs)
        with open(os.path.join(cwd, "SPEC.md"), "w",
                  encoding="utf-8") as _fh:
            _fh.write(_spec_doc)
        yield {"type": "thought", "role": "planner",
               "text": "Wrote SPEC.md (single-task plan) — the run "
                       "builds and is verified against it."}
    except Exception as _sexc:  # noqa: BLE001
        yield {"type": "thought", "role": "planner",
               "text": f"⚠ SPEC.md write failed: {_sexc}"}
    # Best-of-N (Gap C, opt-in): when AIFORGE_BEST_OF_N is set, run the
    # single task N independent times in isolated worktrees, grade each,
    # keep the best. Otherwise fall back to the sequential team pipeline
    # so the user always gets a result. Default flow (flag unset) is
    # unchanged.
    if os.environ.get("AIFORGE_BEST_OF_N"):
        from aiforge_core.runtime import best_of_n as _bon
        _af_log.info("parallel decompose <2 subtasks — best-of-N route")
        _path["parallel"] = True
        # Best-of-N never drains the steer queue (nothing in
        # best_of_n.py touches chat_interject), so mark the run
        # UNSTEERABLE before dispatching. Without this the /steer
        # endpoint answered {"queued": true}, the UI rendered the
        # steer as accepted, and the message was silently dropped at
        # end of turn — the endpoint's own docstring promises it
        # reports "unsupported" instead.
        from aiforge_core.runtime import chat_interject as _chat_interject
        _chat_interject.set_steerable(session_id, False)
        yield from _bon.stream_best_of_n(_with_resume(_spec), cwd,
                                         session_id=session_id)
        # stream_best_of_n emits no terminal `done`; synthesize one so a
        # UI waiting on `done` doesn't hang (exactly one).
        yield {"type": "done"}
        _pctx["done"] = True
        return
    _af_log.info("parallel decompose <2 subtasks — sequential fallback")


def _doc_task_route(prompt, cwd, session_id, _with_resume, pctx):
    """Route a doc/analysis task. Multi-repo analysis fans OUT (one read-only
    explore agent per repo, then synthesize); a single repo naming MANY files is
    PLANNED into bounded read-only groups; otherwise it falls through to the
    single research agent (yielding only the router notice). READ-ONLY + bounded,
    so no _psub_on gate. Sets ``pctx["done"]`` on the fan-out / planned returns."""
    from aiforge_core.runtime import analysis_pipeline as _ap
    # Multi-repo analysis fans OUT (one read-only explore agent per repo,
    # in parallel, then synthesize a draft). A single-repo/topic analysis
    # or a plain doc task stays on the single research agent below.
    try:
        from aiforge_core.runtime import analysis_pipeline as _ap
        _fan, _ana_repos, _ana_topics = _ap.should_fan_out(prompt, cwd)
    except Exception:  # noqa: BLE001 — never break routing on the probe
        _fan, _ana_repos, _ana_topics = (False, [], [])
    # No _psub_on gate: analysis fan-out is READ-ONLY + bounded, and its
    # concurrency already respects AIFORGE_PARALLEL_SUBTASKS_MAX (=1 →
    # sequential). Gating on _psub_on left a single agent seeing only
    # cwd, silently dropping the other repos.
    if _fan:
        yield from _ap.stream_analysis_team(
            _with_resume(prompt), cwd=cwd, session_id=session_id,
            repos=_ana_repos, topics=_ana_topics)
        pctx["done"] = True
        return
    # Single repo but the task names MANY real files → PLAN it into
    # bounded read-only groups (discover→batch-read→synthesize), one
    # explore agent each. A flat many-file analysis is exactly what a
    # local model can't track on one agent; planning keeps every step
    # inside its ceiling. Disable with AIFORGE_ANALYSIS_MIN_FILES=999.
    try:
        _plan, _ana_groups, _ana_topics2 = _ap.plan_single_repo(prompt, cwd)
    except Exception:  # noqa: BLE001 — never break routing on the probe
        _plan, _ana_groups, _ana_topics2 = (False, [], [])
    if _plan:
        _nfiles = sum(len(g.get("files") or []) for g in _ana_groups)
        yield {"type": "thought", "role": "router",
               "text": (f"Doc/analysis on one repo spanning {_nfiles} "
                        f"files — planning into {len(_ana_groups)} bounded "
                        "read-only groups (discover → batch-read → "
                        "synthesize), one explore agent each, so a local "
                        "model never faces a flat multi-file sweep.")}
        yield from _ap.stream_analysis_planned(
            _with_resume(prompt), cwd=cwd, session_id=session_id,
            groups=_ana_groups, topics=_ana_topics2)
        pctx["done"] = True
        return
    yield {"type": "thought", "role": "router",
           "text": "Doc/analysis task (analysis or a doc/Confluence "
                   "deliverable) — routing to the single research agent, "
                   "NOT the code build pipeline. No file tree, tests, or "
                   "PR; the output is the analysis/document (draft)."}
# (the team not-route_pipeline notice — approvals-sequential vs in-place
# edit — is emitted above via chat_router's _rd.notice.)
# Review-edits is a simple/plan-only feature (forced on there). Team /
# parallel / best-of-N runners run the full pipeline and don't hold
# edits — left as-is by design, no notice (avoids per-run noise).


def _followup_enhance_chars() -> int:
    """Follow-ups shorter than this skip the enhancer. The prior turns are
    already in the history the agent reads; a classifier LLM call just to
    decide that was itself a 5–10s wait on every message."""
    try:
        return max(0, int(os.environ.get(
            "AIFORGE_CHAT_FOLLOWUP_ENHANCE_CHARS", "500")))
    except (TypeError, ValueError):
        return 500


def _followup_needs_enhance(prompt: str) -> bool:
    """A long or multi-part follow-up still gets a restatement. A short one
    ("tear", "use postgres", "fix the import") does not."""
    p = (prompt or "").strip()
    if len(p) > _followup_enhance_chars():
        return True
    return p.count("\n") >= 8


def _should_skip_enhance(auto_downgraded, route_pipeline, is_build_task,
                         history, prompt) -> bool:
    """Whether the Enhancer can be skipped this turn.

    A short message the agent will answer or ask about is one model call —
    the restatement is the pause before it speaks, and a classifier that
    labelled the remark a build does not change that. A real build (the
    regex, not that label), a long prompt, and the pipeline route still
    enhance. The pipeline calls ``_enhance`` on its own and keeps the
    2048-token budget; this gate only covers the single-agent path.
    ``is_build_task`` stays in the signature so callers that already
    computed it don't have to change; a short non-build ignores it."""
    # A downgrade off the pipeline does not by itself skip the restatement.
    # A long prompt and a real build still enhance; only a short non-build
    # (direct_reply) does. The pipeline enhances on its own at 2048 tokens.
    del history, is_build_task, auto_downgraded
    if route_pipeline:
        return False
    if _followup_needs_enhance(prompt):
        return False
    from aiforge_core.runtime.chat_router import direct_reply
    return direct_reply(prompt or "")


def _plan_mode_route(_pp, _enriched, _enriched_history, cwd, role, session_id,
                     quick):
    """Plan mode: run the plan-mode agent, and emit ``plan_ready`` BEFORE the
    terminal ``done`` reaches the client (hold the done, yield plan_ready,
    release it) so the UI sees the approvable plan — one-click approve sends
    that plan back on the same conversation.

    No planner model call first. That call ran before any plan text, so a
    plan turn waited through two models. The agent writes the plan."""
    from aiforge_core.runtime.chat_agent import run_chat_agent
    # Plan→approve→execute: hand the plan to the UI so the user can
    # one-click approve — which sends that plan on the same conversation.
    # Persisted so the button survives a reload until the plan is acted on.
    # Emit plan_ready BEFORE the agent's terminal `done` reaches the client
    # (hold the `done`, yield plan_ready, then release `done`) so the UI
    # sees the plan, not a finished turn with no plan.
    _pending_done = None
    _no_plan = False
    _plan_text = ""
    for _ev in run_chat_agent(_enriched_history, cwd=cwd, role=role,
                              session_id=session_id, mode="plan",
                              max_steps=_quick_step_cap(quick)):
        if _ev.get("type") == "done":
            _pending_done = _ev
            continue
        # A planning turn that failed ("the model didn't respond"), was
        # stopped, or ended by asking the user something produced no plan —
        # offering "Approve & Execute" then ran an unplanned build.
        if _ev.get("type") in ("error", "stopped") or _ev.get("awaiting_input"):
            _no_plan = True
        if (_ev.get("type") == "message" and _ev.get("text")
                and not _ev.get("awaiting_input")):
            _plan_text = _ev.get("text") or ""
        yield _ev
    if not _no_plan:
        yield {"type": "plan_ready", "spec": _enriched, "plan": _plan_text}
    if _pending_done is not None:
        yield _pending_done


def _classify_needed(_cr, prompt: str) -> bool:
    """Whether a simple-mode turn waits on the task classifier.

    A long prompt: yes. A short one only when the build regex fires and the
    small-chore rule would not keep it on the single agent anyway — the
    regex alone must not escalate a short remark into the pipeline."""
    if not _cr.is_short_prompt(prompt):
        return True
    return bool(_cr.regex_build_fallback(prompt)
                and not _cr.is_small_task(prompt))


def _decide_chat_route(_pp, prompt, agent_mode, team, parallel_team, cwd,
                       history, quick=False, session_id=None,
                       single_agent=False):
    """Gather the (side-effecting) inputs to the task-type router and return its
    decision. The heavy which-path decision is a PURE function in chat_router;
    here we only probe parallel capability, greenfield-ness, follow-up-ness, the
    LLM task class (fresh turns only), and whether Pipeline-approvals force the
    gated sequential path — each failing safe."""
    try:
        psub_on = _pp.enabled()
    except Exception:  # noqa: BLE001
        psub_on = parallel_team
    try:
        from aiforge_core.runtime import turn_router as _tr2
        fresh = not _tr2.is_followup(history)
    except Exception:  # noqa: BLE001
        fresh = True
    # Reading every source file. It changes the route only for a team
    # follow-up that is not already a build: a fresh team turn pipelines
    # either way, and a simple-mode build escalates because it IS a build.
    # A short chat must not pay the walk.
    greenfield = False
    if team and not fresh:
        try:
            greenfield = _pp._is_greenfield(cwd)
        except Exception:  # noqa: BLE001
            greenfield = True
    cat = None
    # A quick turn is one doer: no classifier. A short message the regex
    # does not call a build is also one doer — the classifier is a model call
    # (5–15s) before the agent speaks. A long prompt still classifies, so a
    # build the regex missed is caught. A short prompt the regex DOES call a
    # build classifies too: that regex alone would escalate "create the user
    # through the api" into the build pipeline, and the classifier is its
    # veto. Team mode classifies: that route is the pipeline.
    from aiforge_core.runtime import chat_router as _cr
    _needs_class = team or _classify_needed(_cr, prompt or "")
    # An approved plan is carried out by this agent. Classifying it as a
    # document sends it down the read-only research path, so the plan's
    # edits never happen.
    if fresh and not quick and _needs_class and not single_agent:
        try:
            from aiforge_core.runtime import task_router as _tr
            cat = _tr.classify_task(prompt, history=history, cwd=cwd,
                                    session_id=session_id)
        except Exception:  # noqa: BLE001 — never break routing on the classifier
            cat = None
    team_approvals = bool(team)   # fail safe → gated sequential
    try:
        from aiforge_core.config import approval_settings as _aps
        team_approvals = bool(team and _aps.required("team"))
    except Exception:  # noqa: BLE001
        pass
    return _cr.decide(
        prompt, agent_mode=agent_mode, team=team, psub_on=psub_on,
        greenfield=greenfield, fresh=fresh, cat=cat,
        team_approvals=team_approvals,
        # A QUICK turn is one doer with a step cap by request — never escalated
        # into the build pipeline because its text (e.g. a diff to explain)
        # happens to read like "create the user through the api".
        auto_escalate=(not quick) and (not single_agent)
        and os.environ.get("AIFORGE_AUTO_ESCALATE", "1") not in ("0", "false"))


def _run_capture_pass(_rc, prompt, repo, cwd, session_id):
    """Classify → store one capture, HARD wall-clock bounded so a degraded LLM
    can never stall the turn. Returns ``(cls, stored, intent)`` or None (no
    capture / timeout / "none" category). Recognition-only gate intent — sets NO
    flag; the UI offers an explicit opt-in. Fully fail-open + fail-fast."""
    import concurrent.futures as _cf

    def _capture_pass():
        c = _rc.classify(prompt, repo=repo, session_id=session_id)
        if c.get("category") == "none":
            return None
        stored = _rc.store(c, repo=repo, session_id=session_id, repo_root=cwd)
        return c, stored, _rc.recognize_gate_intent(c)

    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        from aiforge_core.runtime.run_interrupt import STOPPED, wait_future
        budget = float(os.environ.get("AIFORGE_CAPTURE_BUDGET_S", "6"))
        # optional(): abandoned at its budget, the classify must not wait on
        # for a down model in the leaked thread and fire late on recovery.
        from aiforge_core.llm import model_wait
        got = wait_future(ex.submit(model_wait.side_call(_capture_pass)),
                          budget, session_id)
        if got is STOPPED:
            return None
        return got
    except Exception as exc:  # noqa: BLE001 — timeout/any → no capture
        _af_log.debug("rule_capture pass timed out/failed: %s", exc)
        return None
    finally:
        ex.shutdown(wait=False)


def _rule_capture_pass(prompt, cwd, session_id, _pctx):
    """Rule/Memory/Feedback capture (deterministic, always-on) — runs BEFORE any
    agent so a directive/fact/correction stated in passing is captured + applied.
    A pre-filter skips the LLM classify for ordinary turns; the classify itself is
    HARD wall-clock bounded. Yields a ``captured`` event and, for a PURE capture
    with no actionable task, a terminal ack + ``done`` (setting ``pctx["done"]``
    so the caller returns). FAILS OPEN."""
    try:
        from aiforge_core.runtime import rule_capture as _rc
        _repo = _rc.repo_key(cwd) or "repo"
        # PRE-FILTER: only spend an LLM classify when the message carries a
        # preference/directive cue. Ordinary turns ("hi", "fix the bug")
        # skip the classifier entirely — no per-turn LLM cost.
        if _rc.should_classify(prompt):
            _res = _run_capture_pass(_rc, prompt, _repo, cwd, session_id)
            if _res is not None:
                _cls, _stored, _intent = _res
                _ev = {"type": "captured", "id": _stored.get("id"),
                       "category": _cls["category"], "scope": _cls["scope"],
                       "text": _cls.get("canonical", ""), "repo": _repo}
                if _intent:
                    _ev["gate_intent"] = _intent     # UI offers opt-in pill
                yield _ev
                # PURE capture (no actionable task) → brief ack, skip the
                # agent — UNLESS a deterministic actionable-intent backstop
                # fires (e.g. "...and now fix the bug"): never drop a real
                # task on the classifier's say-so.
                if not _cls.get("task_present", True) \
                        and not _rc.looks_actionable(prompt):
                    yield {"type": "message",
                           "text": f"Got it — saved as {_cls['category']} "
                                   f"({_cls['scope']})."}
                    yield {"type": "done"}
                    # Without the flag the producer went on and ran the full
                    # agent on a message with no task in it, and its reply
                    # replaced the ack as the saved answer.
                    _pctx["done"] = True
                    return
    except Exception as _exc:  # noqa: BLE001 — capture must never break a turn
        _af_log.debug("rule_capture pre-agent pass failed: %s", _exc)
