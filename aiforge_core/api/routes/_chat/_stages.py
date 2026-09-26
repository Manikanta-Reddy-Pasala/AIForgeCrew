"""Turn stages: post-run verification, prompt augmentation, enhancement, and
agent dispatch."""
from __future__ import annotations

import os

from . import _team_route
from ._core import (
    _af_log,
)
from ._routing import (
    _doc_task_route,
    _pipeline_route,
    _quick_step_cap,
)
from ._team_route import _team_target_cwd


def _looks_like_analysis(p: str) -> bool:
    """A read-only analysis query ("analyze/explain/how does X work") — the user
    wants an EXPLANATION, not a build. An action verb (fix/create/build…) present
    means it is NOT read-only."""
    import re as _re
    p = (p or "").lower()
    ask = _re.search(r"\b(analy[sz]e|explain|describe|summar[iy][sz]e|"
                     r"review|understand|audit|document|investigate|trace|"
                     r"walk\s*(me)?\s*through|how\s+(does|do|is|are)|"
                     r"what\s+(does|is|are)|why\s+(does|is|are)|where\s+"
                     r"(is|are)|tell me about|show me how)\b", p)
    change = _re.search(r"\b(fix|create|build|implement|add|write|refactor|"
                        r"rename|delete|remove|update|generat|make|"
                        r"modify|patch|scaffold)\b", p)
    return bool(ask and not change)


_SRC_EXTS_VERIFY = (".py", ".java", ".go", ".js", ".mjs", ".ts", ".tsx", ".c",
                    ".cc", ".cpp", ".h", ".hpp", ".rs", ".rb", ".php", ".cs",
                    ".kt", ".swift", ".scala", ".sh")


def _turn_wrote_source(cwd) -> bool:
    """True only if THIS turn created/modified a source file — the signal there's
    something to build+test. Because a pre-turn baseline commit was taken, git
    status reflects only this turn's writes; a JIRA/Q&A/analysis turn touches no
    source. Does NOT fall through to the process-global touched_paths() when git
    is usable (it can hold a PRIOR turn's path and re-trigger the build)."""
    try:
        import subprocess as _sp
        r = _sp.run(["git", "-C", cwd, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return any(ln[3:].strip().endswith(_SRC_EXTS_VERIFY)
                       for ln in (r.stdout or "").splitlines() if ln.strip())
    except Exception:  # noqa: BLE001 — git missing / timeout
        pass
    try:
        from aiforge_core.runtime.doer_tools import touched_paths
        return any(str(p).endswith(_SRC_EXTS_VERIFY) for p in touched_paths())
    except Exception:  # noqa: BLE001
        return False


def _worth_verifying(cwd) -> bool:
    """PROPORTIONALITY: only run the (heavy) build+test+self-heal when there's a
    detectable build/test stack. A doc/config/tiny edit in a repo with no tests +
    no build system gets the Changes diff, not a pointless build. Unsure → verify
    (old behaviour)."""
    try:
        from aiforge_core.runtime.tools.project_runner import _has_tests, detect
        stacks = (detect(cwd) or {}).get("stacks") or []
        if stacks and _has_tests(cwd, stacks):
            return True
        import glob
        return bool(glob.glob(os.path.join(cwd, "**", "test_*.py"), recursive=True)
                    or glob.glob(os.path.join(cwd, "**", "*_test.py"), recursive=True))
    except Exception:  # noqa: BLE001
        return True


def _integration_verify_events(cwd):
    """Build + run the project's integration tests with the pipeline's self-heal
    (rewrite offending files until green, bounded), then yield the report — but
    only when a build/test ACTUALLY ran (ok True/False; ok=None = no markers/
    toolchain, where the Changes diff is the useful output). Never breaks the turn."""
    try:
        from aiforge_core.runtime.parallel_subtasks import _reconcile_integration
        yield {"type": "thought", "role": "verifier",
               "text": "Building + running integration tests…"}
        ires: dict = {}
        yield from _reconcile_integration(cwd, ires)
        rep = ires.get("rep") or {}
        if rep.get("md") and rep.get("ok") is not None:
            # supplementary=True: render the report but DON'T persist it as the
            # agent's own answer.
            yield {"type": "message", "text": rep["md"], "role": "verifier",
                   "supplementary": True}
    except Exception as exc:  # noqa: BLE001 — never break the turn
        _af_log.debug("integration report skipped: %s", exc)


def _post_run_events(prompt, cwd, agent_mode, simple_sha, changes_only=False):
    """After the single agent finishes: (1) build+test+self-heal ONLY when this
    turn wrote source, the mode isn't plan/read-only, it's env-enabled, and
    there's a stack worth verifying; (2) emit a clean PR-style Changes diff (gated
    on not-read-only ONLY — a doc/config edit still shows its diff; _emit_changes
    self-guards an empty diff)."""
    readonly = _looks_like_analysis(prompt)
    # Simple mode does not start a second repair agent after the turn. That
    # agent held the session (the next message got 409) and re-ran a suite
    # the chat agent had already run. _verify_on_final still feeds a real
    # new failure back to the same agent. The changes diff stays.
    if simple_sha and not readonly:
        try:
            from aiforge_core.runtime.parallel_subtasks import _emit_changes
            yield from _emit_changes(cwd, simple_sha, include_worktree=True)
        except Exception as exc:  # noqa: BLE001
            _af_log.debug("simple changes diff skipped: %s", exc)


_DRAFT_ONLY_NOTE = (
    "\n\n---\n[Deliverable = DRAFT ONLY. Produce the analysis/document as "
    "markdown in your final answer. Do NOT publish — do not call "
    "confluence_create/confluence_update/jira_create. Read tools (repos, web, "
    "existing pages) are fine. I will review and post it myself.]")
_PUBLISH_INTENT_RE = (
    r"\b(publish it|publish the (page|doc|report)|go ahead and (publish|post)|"
    r"actually (publish|create the (page|confluence))|post it to confluence)\b")


def _augment_user_turn(m: dict, enriched, resume_brief, prompt, doc_task) -> None:
    """Augment one user turn IN PLACE with the enhancer's interpretation (verbatim
    words kept + a labelled restatement block, resume brief LAST) and, for a doc
    task with no EXPLICIT publish intent, the DRAFT-ONLY guard."""
    raw = (m.get("content") or "").strip()
    # Keep the resume brief LAST: the restatement is of the WHOLE job, so after
    # the brief it made the final thing the model reads "build everything" —
    # directly after "do ONLY what is still missing".
    brief_tail = ""
    if resume_brief and raw.endswith(resume_brief):
        raw = raw[:-len(resume_brief)].rstrip().rstrip("-").rstrip()
        brief_tail = f"\n\n---\n{resume_brief}"
    if enriched and enriched.strip() and enriched.strip() != raw:
        m["content"] = (
            f"{raw}\n\n---\n[Interpreted request — a context-enriched "
            f"restatement; if it conflicts with my words above, my words win:]"
            f"\n{enriched}{brief_tail}")
    elif brief_tail:
        m["content"] = f"{raw}{brief_tail}"
    # DRAFT-ONLY for a doc task — EXPLICIT publish intent only ('post the
    # findings' must NOT flip publishing on); a wrong write to the team wiki is
    # unrecoverable.
    if doc_task:
        import re as _re
        if not _re.search(_PUBLISH_INTENT_RE, (prompt or "").lower()):
            m["content"] += _DRAFT_ONLY_NOTE


def _prelude_notices(resume_brief, cmd_expanded):
    """Small user-facing notices before the agent runs: a resume marker (so a
    retry doesn't look identical to the failed run) and a command-expansion note
    (so the user sees WHY their "/deploy …" became a longer prompt)."""
    if resume_brief:
        yield {"type": "thought", "role": "system",
               "text": "↻ Resuming the stopped turn — carrying over what already "
                       "landed and finishing only what is pending."}
    if cmd_expanded:
        yield {"type": "thought", "role": "command",
               "text": f"Expanded /{cmd_expanded} command template."}


def _enhance_prompt(_pp, prompt, history, cwd, skip_enhance, session_id=None):
    """The enriched spec for this turn. A skippable follow-up uses the raw prompt;
    otherwise the enhancer folds ``history`` INTO the spec (restoring referent
    resolution — "no, use postgres instead" must resolve against prior turns, not
    fabricate a context-free spec) with recall SCOPED to this session's repo
    (anti-contamination). While the enhancer's LLM call runs, the chat agent's
    session-start memory recall is prefetched (it keys on the raw words, not on
    this spec)."""
    if skip_enhance:
        return prompt
    from aiforge_core.runtime import chat_cancel
    if session_id is not None and chat_cancel.is_cancelled(session_id):
        return prompt
    from aiforge_core.runtime.chat_agent import _chat_repo_key as _crk2
    from aiforge_core.runtime.chat_agent._context import _recall_prefetch

    def _prefetch():
        _recall_prefetch.start(history, cwd, session_id)

    return _pp._enhance(prompt, history=history, cwd=cwd, repo=_crk2(cwd),
                        on_context=_prefetch, session_id=session_id,
                        max_tokens=_chat_enhancer_max_tokens())


def _chat_enhancer_max_tokens() -> int:
    """Chat turns restate; they do not need a 2048-token spec. A cut-off
    rewrite is rejected by the degenerate-spec guard and the raw prompt is
    used, so a smaller budget only ever saves time."""
    try:
        _cap = int(os.environ.get("AIFORGE_CHAT_ENHANCER_MAX_TOKENS", "512"))
    except (TypeError, ValueError):
        _cap = 512
    return max(64, _cap)


def _dispatch_agent_route(_rd, _pp, prompt, cwd, session_id, history,
                          _with_resume, _path, _turn_t0, team, _resume_brief,
                          rctx):
    """Dispatch to the doc-analysis, orchestrator-pipeline, or sequential-team
    route (in that precedence). Yields each route's events and sets
    ``rctx["done"]`` when one of them terminates the turn; falls through (no
    done) to the single-agent path for simple/plan work."""
    _doc_task = _rd.doc_task
    _route_pipeline = _rd.route_pipeline
    if _rd.notice:
        yield {"type": "thought", "role": "router", "text": _rd.notice}
    # A team run's branch + worktree is cleaned up whatever happens: an
    # exception or a client disconnect closes it here (no yield), a finished
    # turn closes it — or keeps it for "continue" (see _team_route.finish_run).
    try:
        if (_route_pipeline or team) and not _doc_task:
            cwd = yield from _team_target_cwd(prompt, history, cwd, rctx,
                                              session_id,
                                              sequential=not _route_pipeline)
            prompt, history = _team_route.localize(rctx, prompt, history)
        if not rctx["done"]:
            yield from _team_route.watch(rctx, _dispatch_routes(
                _rd, _pp, prompt, cwd, session_id, history, _with_resume,
                _path, _turn_t0, team, _resume_brief, rctx))
    except BaseException:
        _team_route.abort_run(rctx)
        raise
    if rctx["done"]:
        yield from _team_route.finish_run(rctx, session_id)


def _dispatch_routes(_rd, _pp, prompt, cwd, session_id, history, _with_resume,
                     _path, _turn_t0, team, _resume_brief, rctx):
    """The doc-analysis, pipeline and sequential-team routes, in that order."""
    _doc_task = _rd.doc_task
    _route_pipeline = _rd.route_pipeline
    if _doc_task:
        yield from _doc_task_route(prompt, cwd, session_id, _with_resume, rctx)
        if rctx["done"]:
            return
    if _route_pipeline:
        yield from _pipeline_route(_pp, prompt, cwd, session_id, history,
                                   _with_resume, _path, _turn_t0, rctx)
        if rctx["done"]:
            return
    if team and not _doc_task:
        # Sequential team pipeline has its own ADK enhancer; don't double-
        # enhance. Mark the driver launched ONLY here so a crash in the
        # parallel pre-steps above still persists + cleans up inline in the
        # producer's finally. A DOC/ANALYSIS task falls through to the single
        # research agent below even in team mode. The resume brief goes in as
        # its own argument so raw_prompt stays the user's actual request.
        _path["driver"] = True
        from aiforge_core.runtime.chat_pipeline import stream_chat_pipeline
        # True when the driver posted + persisted the answer and only its
        # Learner is still running: the producer then ends the chat run.
        _path["handed_off"] = yield from stream_chat_pipeline(
            prompt, cwd=cwd, session_id=session_id, history=history,
            started_at=_turn_t0, resume_brief=_resume_brief)
        # The team run IS the turn: without this the producer fell through and
        # ran the same request again with the single agent (enhancer, baseline
        # commit, run_chat_agent, post-run checks) after the team had finished.
        rctx["done"] = True


def _early_route_events(cmd_help_text, body, history, cwd, role, session_id, pctx):
    """The two deterministic early routes that bypass the whole agent machinery:
    built-in /help (inline command listing, no model call) and BUILDER mode
    (job|skill|workflow|rule — a focused interactive builder; the enhancer would
    distort its clarifying Q&A). Each sets ``pctx["done"]`` so the caller returns."""
    from aiforge_core.runtime.chat_agent import run_chat_agent
    # Built-in /help (or /commands): answer inline with the command listing
    # and finish — no model call, works with zero user command files.
    if cmd_help_text is not None:
        yield {"type": "message", "text": cmd_help_text}
        yield {"type": "done"}
        pctx["done"] = True
        return
    # Builder mode (job|skill|workflow|rule): a focused, deterministic
    # interactive builder. Bypass the enhancer/team/plan machinery (the
    # enhancer would distort the clarifying Q&A) and run the single chat
    # agent with the task charter, which ends by calling its finalize tool.
    if body.builder:
        # NOTE: do NOT re-import run_chat_agent here — a local import inside
        # this generator makes the name LOCAL to the whole generator, so the
        # non-builder paths below (which don't run this branch) hit it
        # unbound → "UnboundLocalError: run_chat_agent". Use the closure from
        # the outer function's import.
        from aiforge_core.runtime.prompts_extended import builders as _bld
        if _bld.charter_for(body.builder):
            yield from run_chat_agent(history, cwd=cwd, role=role,
                                      session_id=session_id, mode="act",
                                      builder=body.builder)
            pctx["done"] = True


def _fold_enriched_history(history, enriched, resume_brief, prompt, doc_task):
    """Fold the enhancer's interpretation into the LAST user turn — AUGMENT, don't
    replace: keep the user's verbatim words and attach the restatement as a
    labelled block the model can cross-check (a distorted enhancement no longer
    silently becomes the request). Returns the new history list."""
    enriched_history = [dict(m) for m in history]
    for m in reversed(enriched_history):
        if m.get("role") == "user":
            _augment_user_turn(m, enriched, resume_brief, prompt, doc_task)
            break
    return enriched_history


def _single_agent_events(enriched_history, cwd, role, session_id, single_mode,
                         quick, awaiting_ctx):
    """Run the single conversational agent and yield its events, flagging
    ``awaiting_ctx["awaiting"]`` when the turn ended waiting on user input (an
    ASK / a REJECT). A doc/analysis task runs read-only (mode="analyze")."""
    from aiforge_core.runtime.chat_agent import run_chat_agent
    for ev in run_chat_agent(enriched_history, cwd=cwd, role=role,
                             session_id=session_id, mode=single_mode,
                             max_steps=_quick_step_cap(quick)):
        if ev.get("type") == "message" and ev.get("awaiting_input"):
            awaiting_ctx["awaiting"] = True
        yield ev


def _commit_simple_baseline(cwd):
    """Commit the CURRENT working-tree state as this turn's diff baseline so the
    single-agent Changes view + the "did it write source?" gate reflect ONLY what
    THIS turn does — a reused chat/ticket workspace carries a previous task's
    uncommitted files otherwise. A jira/confluence/web context folder holds a
    generated dossier, NOT code, so it is SKIPPED (no Changes view). Returns
    ``(simple_sha, skip_worktree)``."""
    _simple_sha = ""
    # A jira/confluence context folder holds a generated dossier + notes, NOT
    # code — code work for a ticket lives in the resolved repo, never here. So
    # never show a Changes view for it: a plain READ writes ticket.md /
    # dossier.md / attachments/ (+ the .gitignore) and would otherwise report
    # "N files changed". Skip the worktree baseline + the changes event for
    # ANY such context, even one already git-inited by an earlier turn.
    # Real repos / repo-context / session scratch still track normally.
    _skip_worktree = False
    try:
        from aiforge_core.runtime import work_context as _wc0
        _ctx0 = _wc0.context_for_path(cwd)
        if _ctx0 and _ctx0[0] in ("jira", "confluence", "web"):
            _skip_worktree = True
            _af_log.info("chat: no Changes view for %s dossier folder %s "
                         "(read-only context)", _ctx0[0], cwd)
    except Exception:  # noqa: BLE001
        pass
    if not _skip_worktree:
        try:
            from aiforge_core.runtime.parallel_subtasks import _commit_turn_baseline
            _simple_sha = _commit_turn_baseline(cwd)
        except Exception:  # noqa: BLE001
            _simple_sha = ""
    # A doc/analysis task is READ-ONLY: force analyze mode so the single
    # agent (like the fan-out explores) can't write/patch/bash in the user's
    # real repo — it produces the analysis/document as its answer. Otherwise
    # a "analyze X and write a report" turn ran writable and could mutate the
    # repo + trigger the post-run build. Non-doc turns keep their mode.
    return _simple_sha, _skip_worktree
