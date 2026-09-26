"""ADK run drivers.

The context-filter plugin wiring plus the run entrypoints that actually
spin an ADK ``Runner``: the full SequentialAgent pipeline
(:func:`_run_pipeline`) and the standalone post-PR live verifier
(:func:`_run_live_verifier` / :func:`_run_single_agent`). Split out of the
orchestrator so the ticket loop stays a thin driver. Context trimming lives in
``_context``; rules, preferences, ticket state and the run config (with the
ambiguous-rule notice) in ``_run_inputs``.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from ..pipeline import build_pipeline
from ._base import log, tickets_mod  # noqa: F401  # tests reach tickets_mod here
from ._context import (  # noqa: F401  # re-exported
    _as_events,
    _cap_content,
    _capped_part,
    _condensing_filter,
    _content_chars,
    _context_window,
    _CtxLimits,
    _dedupe_adjacent_user,
    _history_frac,
    _int_env,
    _phantom_tool_guard,
    _run_repo_root,
    _shorten,
    _tail_trimmer,
    _text_of,
    _window,
)
from ._run_inputs import (  # noqa: F401  # re-exported
    _collect_repo_rules,
    _emit_ambiguous_rule_notice,
    _emit_rules_injected,
    _glob_list,
    _pipeline_run_config,
    _ticket_state,
    _toolchain_md,
    _user_prefs_md,
    _with_images,
)
from ._verdict import _extract_live_verifier, _pipeline_deadline_s


def _build_context_plugins() -> list:
    """Wire ADK's ``ContextFilterPlugin`` so long-running Doer loops
    don't blow past the LM's context window.

    Without this, ADK accumulates every tool result + LLM response in
    session.events and replays the lot on every turn. ONE-117 hit
    MLX GPU OOM (Metal command buffer abort → SIGABRT) after ~140
    LiteLLM calls because the prompt approached 131K tokens and the
    KV cache + 4-way parallel batches exceeded 96GB unified memory.

    ``num_invocations_to_keep`` keeps the last N invocations verbatim —
    BUT an "invocation" starts at a *human-user* message, and a Workflow
    graph run has exactly ONE (the seed prompt): the invocation trim
    never fires. The real work is done by the content-tail custom
    filter (:func:`_tail_trimmer`): keep the seed user message + the most
    recent ``AIFORGE_CONTEXT_MAX_CONTENTS`` contents. Critical hand-offs
    (plan/context/verdicts) are injected from session state via ``{key?}``
    templating, so trimming old history is safe.

    Env knobs:
      AIFORGE_CONTEXT_MAX_CONTENTS=60      → contents to retain (tail)
      AIFORGE_CONTEXT_KEEP_INVOCATIONS=12  → legacy invocation trim
      AIFORGE_CONDENSER_STRATEGY=          → optional condenser on top
      AIFORGE_CONTEXT_FILTER_DISABLE=1     → opt out (debug only)
    """
    if os.environ.get("AIFORGE_CONTEXT_FILTER_DISABLE", "0") in ("1", "true"):
        return []
    try:
        from google.adk.plugins.context_filter_plugin import (
            ContextFilterPlugin,
        )
        from google.adk.plugins.context_filter_plugin import (
            _adjust_split_index_to_avoid_orphaned_function_responses as _adjust,
        )
        from google.adk.plugins.context_filter_plugin import (
            _is_human_user_content as _is_human,
        )
    except ImportError:
        log.warning("context_filter: ContextFilterPlugin not available — "
                    "ADK older than 2.0b? skipping")
        return []

    lim = _CtxLimits()
    custom = _tail_trimmer(lim, _adjust, _is_human)
    if lim.strategy:
        custom = _condensing_filter(custom, lim.strategy)
        log.info("context_filter: enabled max_contents=%d + condenser=%s",
                 lim.max_contents, lim.strategy)
    else:
        log.info("context_filter: enabled max_contents=%d", lim.max_contents)
    return [ContextFilterPlugin(num_invocations_to_keep=lim.keep_invocations,
                                custom_filter=custom)] + _phantom_tool_guard()


def _run_live_verifier(ticket, pr_url: str) -> dict | None:
    """Run the live_verifier as a standalone single-agent pipeline AFTER
    the PR is open.

    Why standalone instead of a pipeline tail: the deploy recipe needs
    a real ``PR_URL`` to merge + roll out before testing. The seed
    prompt carries the ticket body, the PR URL, and a ``git diff`` stat
    so the verifier knows what changed without inheriting the whole
    SequentialAgent history (which overflowed the model). ``PR_URL`` is
    exported to the process env so the recipe's bash ``$PR_URL`` and the
    ``AIFORGE_AUTO_MERGE`` gate (set by deploy_target) resolve.
    """
    import asyncio as _asyncio

    from ..pipeline import build_live_verifier_agent

    repo_root = os.path.expanduser(os.environ.get(
        "AIFORGE_REPO_ROOT", "~/aiforge_workspace",
    ))
    # Compact diff stat so the verifier knows what to exercise without
    # us replaying the full Doer history.
    diff_stat = ""
    try:
        import subprocess as _sp
        diff_stat = _sp.run(
            ["git", "diff", "--stat", "origin/HEAD...HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=30,
        ).stdout[:1500]
    except Exception:  # noqa: BLE001
        pass

    prev_pr = os.environ.get("PR_URL")
    os.environ["PR_URL"] = pr_url
    try:
        prompt = (
            f"# Ticket {ticket.identifier}\n"
            f"## Title\n{ticket.title}\n\n"
            f"## Body\n{ticket.body or '(no body)'}\n\n"
            f"## PR opened\n{pr_url}\n\n"
            f"## Diff stat (origin/HEAD...HEAD)\n```\n{diff_stat}\n```\n"
        )
        verdict_state = _asyncio.run(_run_single_agent(
            build_live_verifier_agent(getattr(ticket, "project", None)),
            prompt, ticket=ticket,
        ))
        return _extract_live_verifier(verdict_state)
    finally:
        if prev_pr is None:
            os.environ.pop("PR_URL", None)
        else:
            os.environ["PR_URL"] = prev_pr


def _key_stateful_tools(session_id: str) -> None:
    """Key bash / browser / IPython to THIS run (see runtime.run_resources)."""
    from ..run_resources import key_stateful_tools
    key_stateful_tools(session_id)


def _run_kwargs(session_id: str, content) -> dict:
    """Cap LLM calls like the main pipeline — a single agent with bash + a
    retry-heavy deploy/verify recipe (live_verifier) could otherwise spin many
    calls bounded only by the wall-clock timeout."""
    kwargs: dict = {"user_id": "aiforge-runner", "session_id": session_id,
                    "new_message": content}
    try:
        from google.adk.agents.run_config import RunConfig
        kwargs["run_config"] = RunConfig(
            max_llm_calls=int(os.environ.get("AIFORGE_MAX_LLM_CALLS", "600")))
    except Exception as exc:  # noqa: BLE001
        log.debug("single-agent RunConfig unavailable: %s", exc)
    return kwargs


async def _session_state(session_svc, session_id: str) -> dict:
    try:
        session = await session_svc.get_session(
            app_name="aiforge", user_id="aiforge-runner", session_id=session_id)
        return dict(session.state or {})
    except Exception:  # noqa: BLE001
        return {}


def _begin_jobs():
    """Commands this run's Doer hands back still running (run_shell
    check-ins) belong to the run and die with it — however it ends."""
    from aiforge_core.runtime import cmd_jobs
    return cmd_jobs.begin_turn()


def _end_jobs(turn) -> None:
    try:
        from aiforge_core.runtime import cmd_jobs
        killed = cmd_jobs.end_turn(turn)
        if killed:
            log.info("stopped %d command(s) the run left running", killed)
    except Exception:  # noqa: BLE001
        pass


async def _drive_single(runner, session_svc, session_id: str,
                        kwargs: dict) -> dict:
    """Run to completion under the pipeline deadline; on an abort recover the
    PARTIAL state instead of hanging / crashing (mirrors the pipeline path)."""
    deadline = _pipeline_deadline_s()
    cm = (asyncio.timeout(deadline) if deadline and deadline > 0
          else contextlib.nullcontext())
    jobs_turn = _begin_jobs()
    try:
        async with cm:
            async for _event in runner.run_async(**kwargs):
                pass  # intentionally empty: drain the run to completion; the
                # final result is read from the session state below
        return await _session_state(session_svc, session_id)
    except Exception as exc:  # noqa: BLE001 — deadline or max_llm_calls trip
        is_deadline = isinstance(exc, TimeoutError)
        log.warning("single-agent run aborted (%s)%s — partial state",
                    type(exc).__name__, " [deadline]" if is_deadline else "")
        state = await _session_state(session_svc, session_id)
        state["_pipeline_abort"] = ("deadline" if is_deadline
                                    else type(exc).__name__)
        return state
    finally:
        _end_jobs(jobs_turn)


async def _run_single_agent(agent, prompt: str, *, ticket=None) -> dict:
    """Drive a one-agent pipeline and return final session state. Used
    for the post-PR live_verifier — no condenser plugins (single turn)."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as gtypes

    session_svc = InMemorySessionService()
    runner = Runner(agent=agent, app_name="aiforge",
                    session_service=session_svc, auto_create_session=True)
    initial_state: dict[str, Any] = {}
    if ticket is not None:
        initial_state["ticket_identifier"] = getattr(ticket, "identifier", "") or ""
        initial_state["ticket_project"] = getattr(ticket, "project", "") or ""
    session = await session_svc.create_session(
        app_name="aiforge", user_id="aiforge-runner",
        state=initial_state or None)
    content = gtypes.Content(role="user",
                             parts=[gtypes.Part.from_text(text=prompt)])
    _key_stateful_tools(session.id)
    try:
        return await _drive_single(runner, session_svc, session.id,
                                   _run_kwargs(session.id, content))
    finally:
        try:
            from aiforge_core.runtime.tools.bash import destroy_session
            destroy_session(session.id)
        except Exception:  # noqa: BLE001
            pass


async def _drive_pipeline(runner, session_svc, session_id: str,
                          content) -> dict:
    """Run to completion under the deadline; on any abort recover the partial
    state and tag it so the caller treats this as a soft FAIL rather than a hard
    crash. A stuck local Doer that hit the cap (or the wall-clock deadline)
    lands the ticket as blocked with its partial state instead of hanging."""
    kwargs: dict[str, Any] = {"user_id": "aiforge-runner",
                              "session_id": session_id, "new_message": content}
    run_config = _pipeline_run_config()
    if run_config is not None:
        kwargs["run_config"] = run_config
    deadline = _pipeline_deadline_s()
    cm = (asyncio.timeout(deadline) if deadline and deadline > 0
          else contextlib.nullcontext())
    jobs_turn = _begin_jobs()
    try:
        async with cm:
            async for _event in runner.run_async(**kwargs):
                pass    # session.state mutated; drained for completeness
        return await _session_state(session_svc, session_id)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        is_limit = "LlmCallsLimit" in name or "max_llm_calls" in str(exc)
        is_deadline = isinstance(exc, TimeoutError)
        if is_limit:
            _why = " [llm-cap]"
        elif is_deadline:
            _why = " [deadline]"
        else:
            _why = ""
        log.warning("pipeline run aborted (%s)%s — returning partial state",
                    name, _why)
        state = await _session_state(session_svc, session_id)
        # An aborted run must not pass, so the verdict is still "fail" — but
        # this is NOT the Feedback judge's opinion, and it used to be recorded
        # as if it were ("feedback: fail: no rationale provided"), telling the
        # operator the code was judged bad when the model was unreachable.
        # The cause travels with it so the audit row can say what happened.
        state["feedback_verdict"] = "fail"
        state["_pipeline_abort"] = "deadline" if is_deadline else name
        state["_pipeline_abort_detail"] = (
            "llm call cap reached" if is_limit else str(exc))[:300]
        return state
    finally:
        _end_jobs(jobs_turn)


def _destroy_run_resources(session_id: str) -> None:
    """Best-effort cleanup of everything keyed to this run (runtime.run_resources)."""
    from ..run_resources import destroy_run_resources
    destroy_run_resources(session_id)


def _dump_trajectory(session, _ticket, initial_state: dict) -> None:
    """Sub #15: dump the session trajectory to disk for replay-style
    debugging."""
    if os.environ.get("AIFORGE_TRAJECTORY_DUMP", "1") not in ("1", "true"):
        return
    try:
        from aiforge_core.runtime.trajectory import dump_trajectory
        ticket_id = (initial_state.get("ticket_identifier")
                     if initial_state else None) or "unknown"
        dump_trajectory(
            ticket_id, session.id, list(getattr(session, "events", []) or []),
            dict(session.state or {}))
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("trajectory.dump_failed: %s", exc)


async def _run_pipeline(prompt: str, *, skip_researcher: bool = False,
                        ticket=None, memory_md: str = "") -> dict:
    """Drive one ADK pipeline run and return the final session state.

    ``skip_researcher`` lets the caller drop the Researcher step for
    greenfield tickets (see :mod:`researcher_routing`). Passed through
    to :func:`build_pipeline` so the SequentialAgent skips assembling
    that LlmAgent — saves ~5 LM calls and ~4 minutes wall-clock when
    the Researcher would have found nothing relevant.

    ``ticket`` (optional) seeds session.state with identifier + project
    so the Learner's after-callback can write Observation_v2 nodes
    keyed back to the ticket. None = test path.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as gtypes

    scope_seed = (_glob_list((getattr(ticket, "metadata", None) or {})
                             .get("scope_allowlist_globs"))
                  if ticket is not None else [])
    rules_md = _collect_repo_rules(ticket, scope_seed)
    pipeline = build_pipeline(
        skip_researcher=skip_researcher,
        # only skip when the rules will actually be SEEDED (ticket path); a
        # ticket-less run must keep the ctx_conventions branch or it gets
        # neither rules nor conventions.
        skip_conventions=bool(rules_md and ticket is not None),
        project=getattr(ticket, "project", None) if ticket else None)
    session_svc = InMemorySessionService()
    runner = Runner(agent=pipeline, app_name="aiforge",
                    session_service=session_svc, auto_create_session=True,
                    plugins=_build_context_plugins())
    initial_state = (_ticket_state(ticket, scope_seed, rules_md, memory_md)
                     if ticket is not None else {})
    session = await session_svc.create_session(
        app_name="aiforge", user_id="aiforge-runner",
        state=initial_state or None)
    _key_stateful_tools(session.id)
    content = gtypes.Content(role="user",
                             parts=[gtypes.Part.from_text(text=prompt)])
    if ticket is not None:
        content = _with_images(content, ticket, gtypes)
    try:
        return await _drive_pipeline(runner, session_svc, session.id, content)
    finally:
        _destroy_run_resources(session.id)
        _dump_trajectory(session, ticket, initial_state)
