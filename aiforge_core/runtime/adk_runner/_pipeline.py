"""ADK run drivers.

The context-filter plugin wiring plus the three run entrypoints that
actually spin an ADK ``Runner``: the full SequentialAgent pipeline
(:func:`_run_pipeline`), the standalone post-PR live verifier
(:func:`_run_live_verifier` / :func:`_run_single_agent`), and the
ambiguous-rule notice emitter. Split out of the orchestrator so the
ticket loop stays a thin driver.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from ..pipeline import build_pipeline
from ._base import log, tickets_mod
from ._verdict import _extract_live_verifier, _pipeline_deadline_s


class _CtxLimits:
    """The trimming budget, read from env once instead of by each closure."""

    __slots__ = ("keep_invocations", "max_contents", "strategy",
                 "max_chars", "max_part_chars", "min_keep")

    def __init__(self) -> None:
        self.keep_invocations = _int_env("AIFORGE_CONTEXT_KEEP_INVOCATIONS", 12)
        self.max_contents = _int_env("AIFORGE_CONTEXT_MAX_CONTENTS", 60)
        self.strategy = os.environ.get("AIFORGE_CONDENSER_STRATEGY", "").strip()
        # ~4 chars/token; budget in tokens then converted to a char ceiling.
        max_tokens = _int_env("AIFORGE_CONTEXT_MAX_TOKENS",
                              int(_context_window() * _history_frac()))
        self.max_chars = max(4000, max_tokens * 4)
        self.max_part_chars = _int_env("AIFORGE_CONTEXT_MAX_PART_CHARS", 24000)
        self.min_keep = max(4, _int_env("AIFORGE_CONTEXT_MIN_KEEP", 8))


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _context_window() -> int:
    try:
        from aiforge_core.config import runtime_settings as _rs
        return int(_rs.get("context_window") or 131072)
    except Exception:  # noqa: BLE001
        return 131072


def _history_frac() -> float:
    """The same condense trigger the simple ReAct loop uses.

    Team mode and mid-run steers then condense EARLY too, instead of running
    near-full where small models drift and invent edits. Soft-fails to the old
    0.55 when the import is unavailable.
    """
    try:
        from aiforge_core.runtime.chat_agent._context._window import (
            _history_fraction)
        return _history_fraction()
    except Exception:  # noqa: BLE001
        return 0.55


def _text_of(c) -> str:
    try:
        return " ".join(p.text for p in (c.parts or [])
                        if getattr(p, "text", None))
    except Exception:  # noqa: BLE001
        return ""


def _dedupe_adjacent_user(contents: list) -> list:
    """Drop adjacent duplicate user-text contents. single_turn nodes
    append their seed input to the SHARED session events (shallow
    session copy in ADK's wrapper), so every chat agent replays the
    ticket+memory seed twice back-to-back — pure token waste."""
    out: list = []
    for c in contents:
        dup = (out and getattr(c, "role", "") == "user"
               and getattr(out[-1], "role", "") == "user"
               and _text_of(c) and _text_of(c) == _text_of(out[-1]))
        if not dup:
            out.append(c)
    return out


def _content_chars(c) -> int:
    """Estimate a content's character weight — text parts plus a rough
    size for function_response payloads (the big tool results)."""
    total = 0
    for p in (getattr(c, "parts", None) or []):
        t = getattr(p, "text", None)
        if t:
            total += len(t)
            continue
        fr = getattr(p, "function_response", None)
        if fr is not None:
            with contextlib.suppress(Exception):
                total += len(str(getattr(fr, "response", "") or ""))
    return total


def _shorten(s: str, cap: int) -> str:
    """Head + tail of ``s``, middle elided, when it exceeds ``cap``."""
    if len(s) <= cap:
        return s
    half = max(1000, cap // 2)
    return (s[:half] + f"\n…[truncated {len(s) - cap} "
            f"chars to fit context]…\n" + s[-half:])


def _capped_part(p, cap: int, gtypes):
    """``p`` truncated to ``cap`` if it is oversized text or a fat function
    response; the part itself otherwise."""
    t = getattr(p, "text", None)
    if t and len(t) > cap:
        return gtypes.Part.from_text(text=_shorten(t, cap))
    fr = getattr(p, "function_response", None)
    if fr is None:
        return p
    resp = getattr(fr, "response", None)
    if len(str(resp or "")) <= cap or not isinstance(resp, dict):
        return p
    half = max(1000, cap // 2)
    trimmed = {k: (_shorten(v, cap) if isinstance(v, str) and len(v) > half else v)
               for k, v in resp.items()}
    return gtypes.Part.from_function_response(
        name=getattr(fr, "name", "") or "", response=trimmed)


def _cap_content(c, cap: int):
    """Return ``c`` if within the per-content cap, else a rebuilt copy
    with oversized text / function_response payloads truncated (head +
    tail kept, middle elided). Falls back to the original on any error
    so a structure we don't understand is never dropped."""
    if cap <= 0 or _content_chars(c) <= cap:
        return c
    try:
        from google.genai import types as gtypes
        parts = [_capped_part(p, cap, gtypes)
                 for p in (getattr(c, "parts", None) or [])]
        return gtypes.Content(role=getattr(c, "role", "user"), parts=parts)
    except Exception:  # noqa: BLE001
        return c


def _window(contents: list, n: int, adjust, is_human) -> list:
    """The seed user message plus the last ``n`` contents, split adjusted so a
    function response is never orphaned from its call."""
    if n <= 0 or len(contents) <= n:
        return list(contents)
    split = len(contents) - n
    with contextlib.suppress(Exception):
        split = adjust(contents, split)
    head_seed = [c for c in contents[:split] if is_human(c)][:1]
    return head_seed + list(contents[split:])


def _tail_trimmer(lim: "_CtxLimits", adjust, is_human):
    """ADK ``custom_filter``: dedupe seed echoes, cap oversized contents, then
    keep the seed user message + the most recent contents under BOTH the count
    cap and the token budget (item-3: protect slow 120B models)."""
    def _tail_trim(contents):
        contents = [_cap_content(c, lim.max_part_chars)
                    for c in _dedupe_adjacent_user(contents)]
        keep_n = lim.max_contents if lim.max_contents > 0 else len(contents)
        out = _window(contents, keep_n, adjust, is_human)
        # Token-budget pass: if the kept window is still too heavy, shrink
        # the tail window until under the char ceiling (or we hit min_keep).
        while (lim.max_chars > 0 and keep_n > lim.min_keep
               and sum(_content_chars(c) for c in out) > lim.max_chars):
            keep_n -= 4
            out = _window(contents, keep_n, adjust, is_human)
        return out
    return _tail_trim


def _as_events(contents: list) -> list[dict]:
    return [{"type": "content", "role": getattr(c, "role", ""),
             "text": " ".join(getattr(p, "text", "") or ""
                              for p in (getattr(c, "parts", None) or [])
                              if getattr(p, "text", None))}
            for c in contents]


def _condensing_filter(tail_trim, strategy: str):
    """Sub #4: optional aggressive condenser layered over the content-tail
    trim. ``amortized`` compresses the oldest half into one synthetic block;
    ``recent`` is keep-tail only."""
    from aiforge_core.runtime.condensers import condense

    def _filter(contents):
        contents = tail_trim(contents)
        condensed = condense(_as_events(contents), strategy)
        # ADK custom_filter must return list[Content]; align tail-N of the
        # condensed events to tail-N of the real contents and keep those
        # objects. ``amortized`` prepends one synthetic block, which we pass
        # through as a fresh Content.
        summarised = bool(condensed) and condensed[0].get("role") == "condenser"
        keep_n = len(condensed) - (1 if summarised else 0)
        tail = list(contents[-keep_n:]) if keep_n > 0 else []
        if not summarised:
            return tail
        from google.genai import types as gtypes
        summary = gtypes.Content(
            role="user",
            parts=[gtypes.Part.from_text(text=condensed[0]["text"])])
        return [summary] + tail
    return _filter


def _phantom_tool_guard() -> list:
    """Keep the pipeline alive when a text agent emits a hallucinated
    function_call — ADK would otherwise raise "Tool X not found" and abort the
    whole run. See tool_error_plugin."""
    try:
        from ..tool_error_plugin import PhantomToolGuardPlugin
        return [PhantomToolGuardPlugin()]
    except Exception:  # noqa: BLE001 — resilience is best-effort
        return []


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
            _adjust_split_index_to_avoid_orphaned_function_responses as _adjust,
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


async def _run_single_agent(agent, prompt: str, *, ticket=None) -> dict:
    """Drive a one-agent pipeline and return final session state. Used
    for the post-PR live_verifier — no condenser plugins (single turn)."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as gtypes

    session_svc = InMemorySessionService()
    runner = Runner(
        agent=agent, app_name="aiforge",
        session_service=session_svc, auto_create_session=True,
    )
    initial_state: dict[str, Any] = {}
    if ticket is not None:
        initial_state["ticket_identifier"] = getattr(ticket, "identifier", "") or ""
        initial_state["ticket_project"] = getattr(ticket, "project", "") or ""
    session = await session_svc.create_session(
        app_name="aiforge", user_id="aiforge-runner",
        state=initial_state or None,
    )
    content = gtypes.Content(
        role="user", parts=[gtypes.Part.from_text(text=prompt)],
    )
    # Key the stateful tools (bash session, browser context, IPython kernel) to
    # THIS run so the destroy_* calls below actually match them — otherwise each
    # mints a per-call id and leaks (browser/ipython did exactly this).
    for _mod, _fn in (("bash", "set_run_id"), ("browser", "set_run_id"),
                      ("ipython_kernel", "set_run_id")):
        try:
            import importlib
            getattr(importlib.import_module(
                f"aiforge_core.runtime.tools.{_mod}"), _fn)(session.id)
        except Exception:  # noqa: BLE001
            pass
    try:
        # Cap LLM calls like the main pipeline — a single agent with bash + a
        # retry-heavy deploy/verify recipe (live_verifier) could otherwise spin
        # many calls bounded only by the wall-clock timeout.
        _sa_kwargs: dict = {"user_id": "aiforge-runner",
                            "session_id": session.id, "new_message": content}
        try:
            from google.adk.agents.run_config import RunConfig
            _sa_kwargs["run_config"] = RunConfig(
                max_llm_calls=int(os.environ.get("AIFORGE_MAX_LLM_CALLS", "600")))
        except Exception as exc:  # noqa: BLE001
            log.debug("single-agent RunConfig unavailable: %s", exc)
        _deadline = _pipeline_deadline_s()
        _cm = (asyncio.timeout(_deadline) if _deadline and _deadline > 0
               else contextlib.nullcontext())
        try:
            async with _cm:
                async for event in runner.run_async(**_sa_kwargs):
                    if event.is_final_response():
                        pass
            session = await session_svc.get_session(
                app_name="aiforge", user_id="aiforge-runner",
                session_id=session.id,
            )
            return dict(session.state or {})
        except Exception as exc:  # noqa: BLE001
            # Overall-deadline timeout or max_llm_calls trip — recover partial
            # state instead of hanging / crashing (mirrors the pipeline path).
            is_deadline = isinstance(exc, TimeoutError)
            log.warning("single-agent run aborted (%s)%s — partial state",
                        type(exc).__name__,
                        " [deadline]" if is_deadline else "")
            try:
                session = await session_svc.get_session(
                    app_name="aiforge", user_id="aiforge-runner",
                    session_id=session.id,
                )
                state = dict(session.state or {})
            except Exception:  # noqa: BLE001
                state = {}
            state["_pipeline_abort"] = "deadline" if is_deadline else \
                type(exc).__name__
            return state
    finally:
        try:
            from aiforge_core.runtime.tools.bash import destroy_session
            destroy_session(session.id)
        except Exception:  # noqa: BLE001
            pass


def _emit_ambiguous_rule_notice(ticket, ambiguous: list) -> None:
    """Autonomous tickets never block on an ambiguous rule match (an
    interactive ticket already got asked via clarify.py before this code
    runs) — best-guess is already baked into rules_md by collect_or_ask;
    this only surfaces a visible, non-blocking notice on the trace."""
    if not ambiguous:
        return
    md = getattr(ticket, "metadata", None) or {}
    if md.get("interactive"):
        return
    for group in ambiguous:
        names = " or ".join(f"'{r.name}'" for r in group)
        try:
            tickets_mod.add_event(
                ticket.id, "pipeline", "ambiguous_rule_match",
                f"Matched rules ambiguous: {names} — picked highest-priority, "
                f"say so if wrong.", {"candidates": [r.name for r in group]})
        except Exception as exc:  # noqa: BLE001
            # This notice is the ONLY human-visible signal an autonomous
            # ticket's ambiguous match ever produces — log loud (not the
            # collect_or_ask wrapper's debug level) and keep processing the
            # remaining groups rather than aborting the whole loop.
            log.warning("ambiguous_rule_match notice failed ticket=%s: %s",
                       getattr(ticket, "identifier", ticket.id), exc)


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

    # Glob-scoped repo rules (Cursor-style): collected BEFORE the build so
    # a repo that carries rules files skips the paid ctx_conventions LLM
    # branch entirely — the rules ARE the conventions, for free.
    _scope_seed: list = []
    if ticket is not None:
        _raw_globs = (getattr(ticket, "metadata", None) or {}).get(
            "scope_allowlist_globs")
        if isinstance(_raw_globs, str):
            _raw_globs = [g.strip() for g in _raw_globs.splitlines()
                          if g.strip()]
        if isinstance(_raw_globs, list):
            _scope_seed = [str(g) for g in _raw_globs if g]
    rules_md = ""
    try:
        from aiforge_core.runtime import repo_rules
        _query = ""
        if ticket is not None:
            _query = (f"{getattr(ticket, 'title', '') or ''}\n"
                      f"{getattr(ticket, 'body', '') or ''}")
        rules_md, _ambiguous_rules = repo_rules.collect_or_ask(
            os.environ.get("AIFORGE_REPO_ROOT", ""), _scope_seed, _query)
        if ticket is not None:
            _emit_ambiguous_rule_notice(ticket, _ambiguous_rules)
    except Exception as exc:  # noqa: BLE001
        log.debug("repo_rules collect failed: %s", exc)

    pipeline = build_pipeline(
        skip_researcher=skip_researcher,
        # only skip when the rules will actually be SEEDED (ticket path);
        # a ticket-less run must keep the ctx_conventions branch or it
        # gets neither rules nor conventions.
        skip_conventions=bool(rules_md and ticket is not None),
        project=getattr(ticket, "project", None) if ticket else None,
    )
    session_svc = InMemorySessionService()
    plugins = _build_context_plugins()
    runner = Runner(
        agent=pipeline, app_name="aiforge",
        session_service=session_svc, auto_create_session=True,
        plugins=plugins,
    )
    initial_state: dict[str, Any] = {}
    if ticket is not None:
        initial_state["ticket_identifier"] = getattr(ticket, "identifier", "") or ""
        initial_state["ticket_project"] = getattr(ticket, "project", "") or ""
        initial_state["ticket_title"] = getattr(ticket, "title", "") or ""
        # RAW ASK for the enhancer degenerate-output guard (pipeline.py):
        # the guard compares state['enhanced_body'] against this and restores
        # it when the rewrite collapsed / dropped every named anchor.
        initial_state["raw_ask"] = ((getattr(ticket, "title", "") or "")
                                    + "\n" + (getattr(ticket, "body", "") or "")).strip()
        # C6 scope enforcement: the UI stores the operator's allowlist in
        # ticket.metadata. Without this seed, scope_guard / verify_scope /
        # the Validator's rule 2 all judged a permanently-empty field.
        _md = getattr(ticket, "metadata", None) or {}
        _globs = _md.get("scope_allowlist_globs")
        if isinstance(_globs, str):
            _globs = [g.strip() for g in _globs.splitlines() if g.strip()]
        _clean: list = []
        if isinstance(_globs, list) and _globs:
            _clean = [str(g) for g in _globs if g]
            initial_state["scope_allowlist_globs"] = _clean
            # Durable copy for plan_promote: replans clear the live key
            # (plan-derived globs are per-plan) but the operator's seed
            # must survive every epoch.
            initial_state["scope_allowlist_globs_seeded"] = list(_clean)
        # Glob-scoped repo rules collected above (pre-build, drives
        # skip_conventions). plan_promote re-matches once the plan
        # widens the globs. Injected via {rules_md?} in prompts.
        if rules_md:
            initial_state["rules_md"] = rules_md
            # Workflow-transparency: record which repo rules applied to this
            # ticket's scope so the Workflow UI can surface them. Best-effort.
            try:
                from aiforge_core.runtime import observability as _obs
                _rule_names = repo_rules.matched_names(
                    os.environ.get("AIFORGE_REPO_ROOT", ""), _scope_seed)
                _tid = getattr(ticket, "id", None)
                if _tid is not None and _rule_names:
                    _obs.emit_context_injected(
                        ticket_id=_tid, agent_role="pipeline",
                        rules=_rule_names)
            except Exception as exc:  # noqa: BLE001
                log.debug("context_injected.emit (rules) failed: %s", exc)
        # Pre-flight memory recall — seeded as STATE, not stitched into
        # the seed prompt: ONE {memory_brief_md?} instruction copy per
        # consuming agent (enhancer/planner/doer/verify_risk) instead
        # of 60-120 history replays. Also replaces the ctx_memory LLM
        # agent, which re-queried the identical backends.
        if memory_md:
            initial_state["memory_brief_md"] = memory_md
        # Host-verified toolchain (python3 vs python, ./mvnw vs mvn, …) —
        # seeded as STATE so the Doer uses the right commands instead of
        # re-discovering them by trial-and-error every ticket. Cheap +
        # cached (shutil.which); soft-fails to nothing.
        try:
            from aiforge_core.config import repo_standards as _rstd
            from aiforge_core.runtime.sandbox import root as _root
            _tb = _rstd.toolchain_brief(str(_root()))
            if _tb:
                initial_state["toolchain_md"] = _tb
        except Exception:  # noqa: BLE001 — never block a run on probing
            pass
        # Durable user preferences (gap #9) — global, cross-repo. Seeded so
        # the agent honours "I always want X" without being re-told. Merge BOTH
        # stores: the Neo4j preferences block (pro backend) AND the embedded
        # sqlite `pref:` units that chat_capture writes — else a preference set
        # in chat on the embedded backend never reached the doer (it writes
        # sqlite, this read only Neo4j).
        try:
            _pref_parts = []
            try:
                from aiforge_core.runtime import user_prefs as _up
                _pb = _up.preferences_block()
                if _pb:
                    _pref_parts.append(_pb)
            except Exception:  # noqa: BLE001
                pass
            try:
                from aiforge_core.runtime.chat_agent import _preferences_context
                _sb = _preferences_context(os.environ.get("AIFORGE_REPO_ROOT") or ".")
                if _sb:
                    _pref_parts.append(_sb)
            except Exception:  # noqa: BLE001
                pass
            if _pref_parts:
                initial_state["user_prefs_md"] = "\n\n".join(_pref_parts)
        except Exception:  # noqa: BLE001
            pass
    session = await session_svc.create_session(
        app_name="aiforge", user_id="aiforge-runner",
        state=initial_state or None,
    )
    # Key the stateful tools to THIS run so their destroy_* (bash/browser/
    # ipython) below match — else each mints a per-call id and leaks.
    for _mod in ("bash", "browser", "ipython_kernel"):
        try:
            import importlib
            importlib.import_module(
                f"aiforge_core.runtime.tools.{_mod}").set_run_id(session.id)
        except Exception:  # noqa: BLE001
            pass
    content = gtypes.Content(
        role="user", parts=[gtypes.Part.from_text(text=prompt)],
    )
    # Sub #6 follow-up: inject multimodal image parts when ticket has
    # image attachments AND the Doer model supports vision.
    if ticket is not None:
        try:
            from aiforge_core.config.agent_config import load_all as get_config
            from aiforge_core.runtime.vision_adk import inject_image_parts

            cfg = get_config()
            doer_model = (cfg.get("doer", {}) or {}).get("model", "")
            md = ticket.metadata or {}
            images = [
                str(f.get("path", "")) for f in (md.get("attached_files") or [])
                if isinstance(f, dict)
                and str(f.get("name", "")).lower().endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".webp"),
                )
                and f.get("path")
            ]
            if images:
                injected = inject_image_parts([content], doer_model, images)
                if injected and injected[0] is not content:
                    content = injected[0]
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.debug("vision_adk.inject failed: %s", exc)
    # Hard ceiling on total LLM calls for the whole pipeline run. A
    # local model (Qwen) can thrash — ONE-7 made 383 calls across 52
    # minutes and wrote ZERO files, spinning on read/think without ever
    # committing an edit. ADK's default cap is high enough that this
    # never tripped. Bounding it means a stuck local Doer aborts (and
    # lands the ticket as blocked) instead of burning an hour. The v6
    # Workflow graph is wider than the old
    # Sequential pipeline — triage + 4 context branches + 3 verifiers +
    # the Doer loop (≤3×) + a possible verifier-replan AND validator-replan
    # each re-running planner/verify/doer. A healthy full+replan run can
    # use ~120-160 calls, so the old 120 ceiling tripped mid-Doer exactly
    # on the harder tickets. 220 leaves headroom. Tune via
    # AIFORGE_MAX_LLM_CALLS.
    run_config = None
    try:
        from google.adk.agents.run_config import RunConfig
        run_config = RunConfig(
            max_llm_calls=int(os.environ.get("AIFORGE_MAX_LLM_CALLS", "600")),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("RunConfig unavailable: %s", exc)
    try:
        _run_kwargs: dict[str, Any] = dict(
            user_id="aiforge-runner",
            session_id=session.id, new_message=content,
        )
        if run_config is not None:
            _run_kwargs["run_config"] = run_config
        _deadline = _pipeline_deadline_s()
        _cm = (asyncio.timeout(_deadline) if _deadline and _deadline > 0
               else contextlib.nullcontext())
        async with _cm:
            async for event in runner.run_async(**_run_kwargs):
                if event.is_final_response():
                    pass  # session.state mutated; drained for completeness
        session = await session_svc.get_session(
            app_name="aiforge", user_id="aiforge-runner",
            session_id=session.id,
        )
        return dict(session.state or {})
    except Exception as exc:  # noqa: BLE001
        # max_llm_calls trip, overall-deadline timeout, or any mid-run ADK
        # error — recover the partial session state and tag it so the caller
        # treats this as a soft FAIL rather than a hard crash. A stuck local
        # Doer that hit the cap (or the wall-clock deadline) lands the ticket
        # as blocked with its partial state instead of hanging forever.
        name = type(exc).__name__
        is_limit = "LlmCallsLimit" in name or "max_llm_calls" in str(exc)
        is_deadline = isinstance(exc, TimeoutError)
        log.warning(
            "pipeline run aborted (%s)%s — returning partial state",
            name,
            " [llm-cap]" if is_limit else (" [deadline]" if is_deadline else ""),
        )
        try:
            session = await session_svc.get_session(
                app_name="aiforge", user_id="aiforge-runner",
                session_id=session.id,
            )
            state = dict(session.state or {})
        except Exception:  # noqa: BLE001
            state = {}
        state["feedback_verdict"] = "fail"
        state["_pipeline_abort"] = "deadline" if is_deadline else name
        return state
    finally:
        # Best-effort tmux session cleanup for the Doer's persistent bash
        # (sub-project #1, see runtime/tools/bash.py). Failure is swallowed
        # so the runner still returns even when tmux isn't installed.
        try:
            from aiforge_core.runtime.tools.bash import destroy_session
            destroy_session(session.id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.debug("bash.destroy_session failed: %s", exc)
        try:
            from aiforge_core.runtime.tools.browser import (
                destroy_context as destroy_browser,
            )
            destroy_browser(session.id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.debug("browser.destroy_context failed: %s", exc)
        try:
            from aiforge_core.runtime.tools.ipython_kernel import (
                destroy_kernel,
            )
            destroy_kernel(session.id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.debug("ipython.destroy_kernel failed: %s", exc)
        try:
            from aiforge_core.runtime.docker_sandbox import (
                destroy_container,
            )
            destroy_container(session.id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.debug("docker_sandbox.destroy_container failed: %s", exc)
        # Sub #15: dump session trajectory for replay-style debugging.
        # Gap-11 (2026-05-23): also index a one-line-per-event summary
        # into AFM as a queryable ``Note_v2`` so future tickets can
        # rerank "have we run something like this before?" against past
        # runs without re-reading raw JSON.
        if os.environ.get("AIFORGE_TRAJECTORY_DUMP", "1") in ("1", "true"):
            try:
                from aiforge_core.runtime.trajectory import (
                    dump_trajectory,
                    index_trajectory_to_memory,
                )
                ticket_id = (initial_state.get("ticket_identifier")
                             if initial_state else None) or "unknown"
                events = list(getattr(session, "events", []) or [])
                dump_out = dump_trajectory(
                    ticket_id, session.id,
                    events, dict(session.state or {}),
                )
                if dump_out.get("ok") and ticket is not None and ticket.project:
                    idx = index_trajectory_to_memory(
                        trajectory_path=dump_out["path"],
                        repo=ticket.project,
                        ticket_identifier=ticket_id,
                    )
                    if not idx.get("ok"):
                        log.debug(
                            "trajectory.index_skipped: %s",
                            idx.get("error", "unknown"),
                        )
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.debug("trajectory.dump_failed: %s", exc)
