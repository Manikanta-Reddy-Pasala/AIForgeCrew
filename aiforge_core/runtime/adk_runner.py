"""Production ticket processor — single-shot ADK pipeline driver.

Polls Postgres for a ``todo`` ticket via
:func:`aiforge_core.tickets.store.claim_next_any`, runs one pass of the
v6 SequentialAgent (see :mod:`pipeline`), then exits. systemd
``Restart=always RestartSec=10`` keeps the loop polling.

Heavy lifting lives in sibling modules so this file stays a thin
orchestrator:

* :mod:`pipeline`     — agent factory + EscalatingLlm wiring
* :mod:`memory_block` — pre-flight AiForgeMemory recall
* :mod:`git_pr`       — auto-commit + push + open-PR helper
* :mod:`prompts`      — per-archetype instruction strings
* :mod:`doer_tools`   — file_read / file_write / run_shell / …

Invoke::

    python -m aiforge_core.runtime.adk_runner
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from aiforge_core.tickets import store as tickets_mod

from . import memory_block
from .git_pr import commit_push_open_pr
from .pipeline import build_pipeline, set_force_provider
from .researcher_routing import should_skip_researcher


def _setup_ticket_workspace(ticket) -> tuple[str | None, dict]:
    """Resolve per-ticket worktree and pin ``AIFORGE_REPO_ROOT`` to it.

    The Doer's sandboxed file tools (:mod:`sandbox.root`) and
    :mod:`git_pr` both read ``AIFORGE_REPO_ROOT`` to choose the working
    directory. Without this hook every ticket lands on whatever the
    operator's systemd EnvironmentFile pinned — usually a stale repo.

    Returns ``(worktree_path, prior_env)``. Caller MUST pass
    ``prior_env`` back to :func:`_restore_env` in a finally block.
    """
    from aiforge_core.runtime.workspace import ensure_branch_and_worktree

    # Capture BOTH env vars we override per-ticket so the finally can
    # restore them. AIFORGE_AFM_REPO scopes memory recall (unified_query
    # afm_bundle/xrepo, memory_lookup, impacted_tests) to THIS ticket's
    # repo — without it those sources fall back to a process-global that
    # is never set (→ dead) or, if exported once, leaks another repo's
    # context into every ticket (the ONE-2 class bug).
    prior = {
        "AIFORGE_REPO_ROOT": os.environ.get("AIFORGE_REPO_ROOT"),
        "AIFORGE_AFM_REPO": os.environ.get("AIFORGE_AFM_REPO"),
        "AIFORGE_CURRENT_TICKET": os.environ.get("AIFORGE_CURRENT_TICKET"),
    }
    project = (getattr(ticket, "project", "") or "").strip()
    if project:
        os.environ["AIFORGE_AFM_REPO"] = project
    # Expose the current ticket so doer tools (e.g. subtask_update) can flip
    # this ticket's internal subtask status as the agent works.
    os.environ["AIFORGE_CURRENT_TICKET"] = getattr(ticket, "identifier", "") or ""
    worktree = ensure_branch_and_worktree(ticket)
    if worktree:
        os.environ["AIFORGE_REPO_ROOT"] = worktree
        log.info("ticket=%s workspace=%s afm_repo=%s",
                 ticket.identifier, worktree, project or "-")
    else:
        log.warning(
            "ticket=%s no worktree (project=%r) — falling back to env",
            ticket.identifier, ticket.project,
        )
    return worktree, prior


def _restore_env(prior) -> None:
    # ``prior`` is the dict captured by _setup_ticket_workspace. Tolerate a
    # bare string for back-compat (older call shape = REPO_ROOT only).
    if isinstance(prior, str) or prior is None:
        prior = {"AIFORGE_REPO_ROOT": prior, "AIFORGE_AFM_REPO":
                 os.environ.get("AIFORGE_AFM_REPO")}
    for key, val in prior.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _materialize_attachments_in_worktree(ticket, worktree: str) -> None:
    """Copy operator-uploaded files into the per-ticket worktree.

    The API persists attachments under its own
    ``AIFORGE_REPO_ROOT/.aiforge/ticket-files/{identifier}/`` (typically
    a shared workspace). The runner then rebinds
    ``AIFORGE_REPO_ROOT`` to a per-ticket git worktree. Without this copy
    the Doer prompt's ``.aiforge/ticket-files/{id}/<name>`` relative
    path resolves to a missing file inside the worktree.

    Strategy: copy each upload by absolute path (stored as ``abs_path``
    at upload time; falls back to the api's historical default base
    for tickets created before that field existed). Skips silently
    when the ticket has no attachments or the worktree is missing.
    """
    import shutil
    if not worktree or not os.path.isdir(worktree):
        return
    md = ticket.metadata or {}
    files = md.get("attached_files") or []
    if not isinstance(files, list) or not files:
        return
    dest_dir = os.path.join(
        worktree, ".aiforge", "ticket-files", ticket.identifier,
    )
    os.makedirs(dest_dir, exist_ok=True)
    fallback_base = os.path.expanduser(os.environ.get(
        "AIFORGE_TICKET_FILES_BASE", "~/codeRepo/Scheduler",
    ))
    copied = 0
    for f in files:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        if not name:
            continue
        src = f.get("abs_path")
        if not src or not os.path.isfile(src):
            rel = f.get("path") or ""
            candidate = os.path.join(fallback_base, rel)
            src = candidate if os.path.isfile(candidate) else None
        if not src:
            log.warning(
                "ticket=%s attachment missing on disk name=%r",
                ticket.identifier, name,
            )
            continue
        try:
            shutil.copy2(src, os.path.join(dest_dir, name))
            copied += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ticket=%s attachment copy failed name=%r: %s",
                ticket.identifier, name, exc,
            )
    if copied:
        log.info(
            "ticket=%s materialized %d attachment(s) into worktree",
            ticket.identifier, copied,
        )


def _persist_ticket_media(ticket) -> None:
    """Gap-10 wire-in: stash image attachments as an AFM
    ``Observation_v2`` with ``media_refs`` populated.

    Vision sub #6 attaches images for the run, but the bytes vanished
    once the ADK session torn down. Capturing the paths here gives a
    durable record so future tickets can recall "we saw screenshot X
    last time" via the same memory_block path the Doer already reads.

    Soft-fail — never raises into the ticket loop. ``AIFORGE_VISION_PERSIST=0``
    opts out.
    """
    if os.environ.get("AIFORGE_VISION_PERSIST", "1") in ("0", "false", ""):
        return
    md = ticket.metadata or {}
    files = md.get("attached_files") or []
    media_paths = [
        str(f.get("path", "")) for f in files
        if isinstance(f, dict)
        and str(f.get("name", "")).lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp"),
        )
    ]
    media_paths = [p for p in media_paths if p]
    if not media_paths:
        return
    if not ticket.project:
        return
    try:
        from aiforge_memory.features.memory.store import upsert_observation
        from neo4j import GraphDatabase
    except ImportError:
        return
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get(
        "AIFORGE_NEO4J_PASSWORD",
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        log.debug("vision persist driver fail: %s", exc)
        return
    try:
        try:
            from aiforge_core.tickets.store import get as ticket_get
            t = ticket_get(ticket.identifier)
            created_at = getattr(t, "created_at", None)
        except Exception:
            created_at = None
        event_time = None
        if created_at is not None:
            try:
                event_time = created_at.timestamp()
            except Exception:
                event_time = None
        _media_text = (
            f"Ticket {ticket.identifier} included "
            f"{len(media_paths)} image attachment(s): "
            + ", ".join(p.rsplit("/", 1)[-1] for p in media_paths)
        )
        # embed so the observation is reachable via vector recall / PPR
        # (was write-only without embed_vec). Soft on sidecar absence.
        _media_vec = None
        try:
            from aiforge_core.memory.embed import embed as _embed
            _media_vec = _embed(_media_text)
        except Exception:  # noqa: BLE001
            _media_vec = None
        upsert_observation(
            drv, repo=ticket.project,
            text=_media_text,
            kind="attachment",
            author="adk_runner",
            tags=[f"ticket:{ticket.identifier}", "kind:vision"],
            media_refs=media_paths,
            event_time=event_time,
            embed_vec=_media_vec,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("vision persist failed: %s", exc)
    finally:
        try:
            drv.close()
        except Exception:
            pass


log = logging.getLogger("adk_runner")
logging.basicConfig(
    level=os.environ.get("AIFORGE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# Map Feedback verdict → tickets-store status. ``fail`` and any
# unrecognised value land in ``blocked`` so a human can triage.
# ``partial`` comes from the loop-budget kill switch (see
# :mod:`loop_budget`) — partial work still gets PR-shipped for human
# review, so the status maps to ``blocked`` to flag triage need.
_VERDICT_TO_STATUS: dict[str, str] = {
    "pass": "done",
    "scope_violation": "cancelled",
    "partial": "blocked",
}


# Order matters: ``scope_violation`` is checked before ``fail`` (string
# substring overlap) and ``partial`` is checked before ``pass`` for
# the same reason — neither is a strict substring of the other today
# but the rule keeps future-proofing cheap.
_VERDICT_TOKENS: tuple[str, ...] = (
    "scope_violation", "partial", "pass", "fail",
)

# Cap rationales persisted to ticket_events so a chatty model can't bloat
# the audit trail with a multi-paragraph rant. 300 chars matches the spec
# in the operator-observability ticket.
_REASON_MAX_CHARS = 300
_REASON_DEFAULT_PASS = "no rationale provided"
_REASON_DEFAULT_FAIL = "no rationale provided"


def _extract_reason(state: dict, verdict: str) -> str:
    """Pull the post-verdict rationale line out of the Feedback output.

    The Feedback prompt asks the model to put the verdict token on
    line 1 and a short rationale on line 2+. This function returns
    that rationale flattened to a single line, trimmed to
    :data:`_REASON_MAX_CHARS` so the audit trail can't be bloated.

    Tolerated shapes mirror :func:`_extract_verdict`:
      1. raw string ``"<token>\\n<reason>..."`` → returns reason
      2. dict ``{"verdict": ..., "rationale": "..."}`` → returns rationale
      3. anything else / no rationale → role-appropriate default

    The verdict token itself is stripped from the head so we don't
    double-print it (``"pass: pass — looks good"`` → ``"pass: looks good"``).
    """
    raw = state.get("feedback_verdict")
    text: str | None = None
    if isinstance(raw, dict):
        # Legacy JSON dict — both ``rationale`` and ``reason`` seen in
        # the wild; ``reason`` is the new canonical key (matches the
        # ticket_events column the operators query).
        text = raw.get("rationale") or raw.get("reason")
    elif isinstance(raw, str) and raw.strip():
        s = raw.strip()
        # Legacy JSON string — try once, fall through to plain text on
        # parse fail rather than 500ing.
        if s.startswith("{"):
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                text = obj.get("rationale") or obj.get("reason")
        if text is None:
            # Plain leading-token format: drop line 1, take the rest.
            head = s.lstrip("`*_-> ")
            for token in _VERDICT_TOKENS:
                if head.lower().startswith(token):
                    head = head[len(token):]
                    break
            # Anything after the verdict token is the rationale; flatten
            # newlines and tabs so the audit row is single-line.
            text = head.strip(" :—-\n\t").replace("\n", " ").replace("\t", " ")

    if not text:
        return _REASON_DEFAULT_PASS if verdict == "pass" else _REASON_DEFAULT_FAIL

    # Collapse runs of whitespace so the audit body is compact.
    text = " ".join(text.split())
    if len(text) > _REASON_MAX_CHARS:
        text = text[: _REASON_MAX_CHARS - 1].rstrip() + "…"
    return text


def _record_verdict_event(ticket_id: int, verdict: str, reason: str) -> None:
    """Persist a ``verdict_attempt`` row in ``ticket_events``.

    Called once per ticket after the SequentialAgent run resolves its
    final session state. The row schema is:

      kind        = 'verdict_attempt'
      agent_role  = 'feedback'
      body        = '<verdict>: <reason>'
      metadata    = {'verdict': ..., 'reason': ...}

    Operators query this row to see WHY a Doer-Feedback loop
    converged (or didn't) — the prior ``status_change`` rows only
    captured the eventual ticket status, not the reasoning.

    Failures are swallowed: the audit trail is best-effort, we never
    want a Postgres hiccup to block the runner from finalising the
    ticket status. The exception is logged so an operator can spot a
    persistent DB-grant problem.
    """
    body = f"{verdict}: {reason}"
    try:
        tickets_mod.add_event(
            ticket_id, "feedback", "verdict_attempt", body,
            {"verdict": verdict, "reason": reason},
        )
    except Exception as exc:  # pragma: no cover — best-effort audit
        log.warning("ticket_id=%s failed to persist verdict_attempt: %s",
                    ticket_id, exc)


def _extract_live_verifier(state: dict) -> dict | None:
    """Pull the live_verifier verdict out of pipeline state.

    The agent is told to emit a fenced ```json``` block at the end of
    its response containing ``{"ok": bool, "rationale": "...", ...}``.
    Parses the LAST such block in ``state['live_verifier_verdict']``.
    Returns ``None`` when the stage didn't run or the JSON couldn't be
    parsed — caller treats that as "no veto" rather than blocking on
    a parser hiccup.
    """
    raw = state.get("live_verifier_verdict")
    if isinstance(raw, dict):
        return raw if "ok" in raw else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    # Strip ```json fences then try whole-text + last balanced object.
    import re as _re
    fenced = _re.findall(
        r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=_re.DOTALL,
    )
    candidates = fenced[::-1]  # prefer the last (final answer)
    if text.startswith("{"):
        candidates.append(text)
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "ok" in obj:
            return obj
    return None


def _extract_verdict(state: dict) -> str:
    """Pull the Feedback verdict out of pipeline state.

    The new Feedback prompt asks for a leading token (``pass`` /
    ``fail`` / ``scope_violation``) followed by an optional rationale
    line — much more robust than strict JSON for local models (qwen
    etc.) which routinely wrap responses in prose.

    Tolerated shapes (in order):
      1. raw string starting with one of the tokens
      2. JSON-with-``verdict``-key (legacy — still emitted by some
         models)
      3. anything else → ``fail``

    ``scope_violation`` is checked before ``fail`` because the literal
    string contains ``fail`` as a substring; without the order rule a
    model that emits ``scope_violation`` would be parsed as ``fail``.
    """
    raw = state.get("feedback_verdict")
    if isinstance(raw, dict):
        return str(raw.get("verdict", "fail")).lower()
    if not isinstance(raw, str):
        return "fail"

    text = raw.strip()
    if not text:
        return "fail"

    # Legacy JSON path — kept for tickets ran on older prompt revisions.
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and obj.get("verdict"):
            return str(obj["verdict"]).lower()

    # New prompt path — leading token decides. Strip any markdown-y
    # wrapping the model might add despite the rules.
    head = text.lstrip("`*_-> ").lower()
    for token in _VERDICT_TOKENS:
        if head.startswith(token):
            return token
    return "fail"


def _extract_verifier(state: dict) -> str | None:
    """Grab the verifier verdict (``pass``/``reject``) when present."""
    raw = state.get("verifier_verdict")
    if isinstance(raw, dict):
        return raw.get("verdict")
    return None


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
    filter below: keep the seed user message + the most recent
    ``AIFORGE_CONTEXT_MAX_CONTENTS`` contents (split adjusted so a
    function response is never orphaned from its call). Critical
    hand-offs (plan/context/verdicts) are injected from session state
    via ``{key?}`` templating, so trimming old history is safe.

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
            _adjust_split_index_to_avoid_orphaned_function_responses,
            _is_human_user_content,
        )
    except ImportError:
        log.warning("context_filter: ContextFilterPlugin not available — "
                    "ADK older than 2.0b? skipping")
        return []
    keep = int(os.environ.get("AIFORGE_CONTEXT_KEEP_INVOCATIONS", "12"))
    max_contents = int(os.environ.get("AIFORGE_CONTEXT_MAX_CONTENTS", "60"))
    strategy = os.environ.get("AIFORGE_CONDENSER_STRATEGY", "").strip()

    def _text_of(c) -> str:
        try:
            return " ".join(p.text for p in (c.parts or [])
                            if getattr(p, "text", None))
        except Exception:
            return ""

    def _dedupe_adjacent_user(contents):
        """Drop adjacent duplicate user-text contents. single_turn nodes
        append their seed input to the SHARED session events (shallow
        session copy in ADK's wrapper), so every chat agent replays the
        ticket+memory seed twice back-to-back — pure token waste."""
        out: list = []
        for c in contents:
            if (out
                    and getattr(c, "role", "") == "user"
                    and getattr(out[-1], "role", "") == "user"):
                t = _text_of(c)
                if t and t == _text_of(out[-1]):
                    continue
            out.append(c)
        return out

    def _tail_trim(contents):
        """Dedupe seed echoes, then keep seed user message + last
        ``max_contents`` contents."""
        contents = _dedupe_adjacent_user(contents)
        if max_contents <= 0 or len(contents) <= max_contents:
            return contents
        split = len(contents) - max_contents
        try:
            split = _adjust_split_index_to_avoid_orphaned_function_responses(
                contents, split)
        except Exception:
            pass
        head_seed = [c for c in contents[:split]
                     if _is_human_user_content(c)][:1]
        return head_seed + list(contents[split:])

    if strategy:
        # Sub #4: optional aggressive condenser layered over the
        # content-tail trim. ``amortized`` compresses oldest half
        # into one synthetic block; ``recent`` is keep-tail only.
        from aiforge_core.runtime.condensers import condense

        def _adk_custom_filter(contents):
            contents = _tail_trim(contents)
            events = [
                {"type": "content", "role": getattr(c, "role", ""),
                 "text": " ".join(
                     getattr(p, "text", "") for p in getattr(c, "parts", [])
                     if getattr(p, "text", None)
                 )}
                for c in contents
            ]
            condensed = condense(events, strategy)
            # ADK custom_filter must return list[Content]; map back by
            # keeping the original Content objects when index survives,
            # otherwise drop (amortized prepends one synthetic block we
            # let through as-is via a fresh Content).
            from google.genai import types as gtypes
            # Heuristic: align tail-N of condensed to tail-N of contents.
            keep_n = len(condensed) - (1 if condensed and condensed[0].get(
                "role") == "condenser" else 0)
            tail = contents[-keep_n:] if keep_n > 0 else []
            if condensed and condensed[0].get("role") == "condenser":
                summary_part = gtypes.Part.from_text(
                    text=condensed[0]["text"],
                )
                summary = gtypes.Content(role="user", parts=[summary_part])
                return [summary] + list(tail)
            return list(tail)

        custom = _adk_custom_filter
        log.info("context_filter: enabled max_contents=%d + condenser=%s",
                 max_contents, strategy)
    else:
        custom = _tail_trim
        log.info("context_filter: enabled max_contents=%d", max_contents)
    return [ContextFilterPlugin(
        num_invocations_to_keep=keep, custom_filter=custom,
    )]


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

    from .pipeline import build_live_verifier_agent

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
    # Key the bash tool's persistent session to THIS run so destroy_session
    # below actually matches it (otherwise bash mints a per-call id and leaks).
    try:
        from aiforge_core.runtime.tools.bash import set_run_id as _bash_set_run_id
        _bash_set_run_id(session.id)
    except Exception:  # noqa: BLE001
        pass
    try:
        async for event in runner.run_async(
            user_id="aiforge-runner",
            session_id=session.id, new_message=content,
        ):
            if event.is_final_response():
                pass
        session = await session_svc.get_session(
            app_name="aiforge", user_id="aiforge-runner",
            session_id=session.id,
        )
        return dict(session.state or {})
    finally:
        try:
            from aiforge_core.runtime.tools.bash import destroy_session
            destroy_session(session.id)
        except Exception:  # noqa: BLE001
            pass


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
        rules_md = repo_rules.collect(
            os.environ.get("AIFORGE_REPO_ROOT", ""), _scope_seed)
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
        # the agent honours "I always want X" without being re-told.
        try:
            from aiforge_core.runtime import user_prefs as _up
            _pb = _up.preferences_block()
            if _pb:
                initial_state["user_prefs_md"] = _pb
        except Exception:  # noqa: BLE001
            pass
    session = await session_svc.create_session(
        app_name="aiforge", user_id="aiforge-runner",
        state=initial_state or None,
    )
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
        async for event in runner.run_async(**_run_kwargs):
            if event.is_final_response():
                pass  # session.state mutated; drained for completeness
        session = await session_svc.get_session(
            app_name="aiforge", user_id="aiforge-runner",
            session_id=session.id,
        )
        return dict(session.state or {})
    except Exception as exc:  # noqa: BLE001
        # max_llm_calls trip (or any mid-run ADK error) — recover the
        # partial session state and tag it so the caller treats this as
        # a soft FAIL rather than a hard crash. A stuck local Doer that
        # hit the cap lands the ticket as blocked with its partial state.
        name = type(exc).__name__
        is_limit = "LlmCallsLimit" in name or "max_llm_calls" in str(exc)
        log.warning(
            "pipeline run aborted (%s)%s — returning partial state",
            name, " [llm-cap]" if is_limit else "",
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
        state["_pipeline_abort"] = name
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


def _build_prompt(ticket, memory_md: str) -> str:
    """Compose the seed prompt for the SequentialAgent."""
    out = (
        f"# Ticket {ticket.identifier}\n"
        f"## Title\n{ticket.title}\n\n"
        f"## Body\n{ticket.body or '(no body)'}\n"
    )
    # Operator follow-up comments: anything the human added via
    # POST /api/tickets/{id}/comments after ticket creation. The
    # Enhancer would otherwise never see this signal — it only
    # reads ``ticket.body``. Folded in chronological order; bot/agent
    # comments are excluded so the Doer doesn't loop on its own
    # past commentary.
    try:
        evts = tickets_mod.comments(ticket.id) or []
        human_comments = [
            e for e in evts
            if e.get("kind") == "comment"
            and (e.get("agent_role") or "").lower() == "human"
            and (e.get("body") or "").strip()
        ]
        if human_comments:
            out += "\n## Operator follow-up comments\n"
            out += (
                "These were posted on the ticket AFTER it was opened. "
                "Treat them as authoritative extensions of the body.\n\n"
            )
            for c in human_comments:
                ts = str(c.get("created_at") or "")[:19]
                body = (c.get("body") or "").strip()
                out += f"- _{ts}_:\n  {body}\n"
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("comment_fold_failed: %s", exc)
    # Ticket attachments — list paths so the Doer can `file_read` them
    # via its file tools. The files are materialized into the worktree
    # (see _materialize_attachments_in_worktree). Each entry is the
    # workspace-relative path the API persisted.
    md = ticket.metadata or {}
    files = md.get("attached_files") or []
    if isinstance(files, list) and files:
        out += "\n## Attached files (read these BEFORE you start)\n"
        for f in files:
            if not isinstance(f, dict):
                continue
            path = f.get("path", "")
            size = f.get("size", "?")
            name = f.get("name", "")
            if path:
                out += f"- `{path}` ({name}, {size} bytes)\n"
        out += (
            "\nThese files were uploaded by the operator with the "
            "ticket. Use `file_read` to load them — their context is "
            "REQUIRED for the change.\n"
        )
    # NOTE: memory_md is NO LONGER appended to the seed. The seed is
    # replayed in contents on every chat-mode LLM call (60-120×/ticket ≈
    # 40-80K tokens of pure memory-block repetition). The block now seeds
    # state['memory_brief_md'] instead → merged once into
    # {context_brief_md?} / {memory_brief_md?} instruction injections,
    # which also survive compaction. (Param retained for call-site
    # compatibility; the runner routes it into initial_state instead.)
    _ = memory_md

    # Skills + microagent injection. Relevance-search the skill registry
    # (SKILL.md playbooks, incl. ones the Doer authored via learn_skill) +
    # always-on repo skills, keyed on ticket title + body. Folds in legacy
    # microagents. Best-effort: parse failures swallowed.
    try:
        from aiforge_core.runtime import skills as _skills
        hay = f"{ticket.title or ''} {ticket.body or ''}"
        sk_block = _skills.auto_context(hay, None)
        if sk_block:
            out = sk_block + "\n\n" + out
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("skills.inject failed: %s", exc)

    # Workflows injection — same treatment as skills, so the pipeline agents
    # see relevant reusable end-to-end procedures (parity with the chat agent).
    try:
        from aiforge_core.runtime import workflows as _workflows
        wf_block = _workflows.auto_context(
            f"{ticket.title or ''} {ticket.body or ''}", None)
        if wf_block:
            out = wf_block + "\n\n" + out
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("workflows.inject failed: %s", exc)

    # Vision attach hint (sub #6). When the ticket has image attachments
    # AND the active Doer model supports vision, list them with a flag so
    # the model knows it can request a multimodal turn. Actual content-
    # block conversion lives in vision.attach_image; wiring it through
    # ADK's LlmRequest.contents shape is a follow-up.
    try:
        from aiforge_core.config.agent_config import load_all as get_config
        from aiforge_core.runtime.vision import supports_vision
        cfg = get_config()
        doer_model = (cfg.get("doer", {}) or {}).get("model", "")
        if supports_vision(doer_model):
            md = ticket.metadata or {}
            images = [
                f for f in (md.get("attached_files") or [])
                if isinstance(f, dict)
                and str(f.get("name", "")).lower().endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".webp"),
                )
            ]
            if images:
                out += (
                    "\n## Multimodal images (vision-enabled model)\n"
                    "These attachments are images. Call `vision.attach_image`\n"
                    "to convert each into multimodal content blocks.\n"
                )
                for img in images:
                    out += f"- `{img.get('path','')}`\n"
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("vision.attach_hint failed: %s", exc)
    return out


def _ticket_force_provider(ticket) -> str | None:
    """Per-ticket pipeline override — pin the whole run onto one provider
    via ``ticket.metadata['force_provider']``.

    Only honours providers that still exist in the registry, so a stale
    ticket carrying a retired provider marker is silently
    ignored (the run falls back to the role's configured model) instead
    of crashing the pipeline.
    """
    md = ticket.metadata or {}
    forced = md.get("force_provider")
    if isinstance(forced, str) and forced:
        try:
            from aiforge_core.config.agent_config import PROVIDERS
            if forced in PROVIDERS:
                return forced
        except Exception:  # noqa: BLE001
            pass
    return None


def _ingest_ticket_external_refs(ticket) -> None:
    """Gap-9 wire-in: feed ``ticket.metadata.external_refs`` (a list
    of URLs / paths) through :func:`ingest_external_source` so the
    Doer's memory_block sees their content.

    Each ref ingests as one ``Doc_v2`` + chunked ``Note_v2`` keyed on
    the ref URI. AFM dedupes by URI at the Doc level (second ingest of
    the same URL re-uses the node), so the cost of re-ingesting is
    bounded.

    Disable with ``AIFORGE_EXTERNAL_INGEST=0``. Soft-fail on any
    backend error.
    """
    if os.environ.get("AIFORGE_EXTERNAL_INGEST", "1") in ("0", "false", ""):
        return
    md = ticket.metadata or {}
    refs = md.get("external_refs") or []
    refs = [r for r in refs if isinstance(r, str) and r.strip()]
    if not refs:
        return
    if not ticket.project:
        return
    try:
        from aiforge_memory.features.external_ingest import (
            ingest_external_source,
        )
        from neo4j import GraphDatabase
    except ImportError:
        return
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get(
        "AIFORGE_NEO4J_PASSWORD",
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        log.debug("external_ingest driver fail: %s", exc)
        return
    try:
        for src in refs[:5]:  # cap fan-out per ticket
            try:
                out = ingest_external_source(
                    drv,
                    source=src,
                    repo=ticket.project,
                    source_type=md.get("external_refs_type", "external"),
                    tags=[f"ticket:{ticket.identifier}"],
                )
                log.info(
                    "external_ingest ticket=%s src=%s ok=%s notes=%d",
                    ticket.identifier, src, out.get("ok"),
                    len(out.get("note_ids") or []),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("external_ingest failed for %s: %s", src, exc)
    finally:
        try:
            drv.close()
        except Exception:
            pass


def _process_one_ticket() -> bool:
    """Claim + run one ticket. Returns True when one ran, False on
    empty queue (caller exits + lets systemd back off)."""
    ticket = tickets_mod.claim_next_any()
    if ticket is None:
        return False

    log.info("claimed ticket=%s title=%r", ticket.identifier, ticket.title)

    # Interactive (chat) runs may pause to ask clarifying questions before
    # any pipeline work. Static tickets skip this entirely. Returns True
    # when it parked the ticket awaiting the user's answer.
    try:
        from .clarify import maybe_clarify
        if maybe_clarify(ticket):
            return True
    except Exception as exc:  # noqa: BLE001
        log.debug("clarify gate skipped: %s", exc)

    # LM Studio liveness check + opportunistic tunnel restart. ONLY
    # relevant when the Doer points at the loopback mlx-lm box
    # (127.0.0.1:1234) — if the operator configured a remote
    # OpenAI-compatible endpoint, that local box is irrelevant and we must
    # NOT probe it. ``AIFORGE_LM_HEALTH=0`` opts out entirely. When the
    # endpoint is unreachable the per-call EscalatingLlm retry chain
    # surfaces the error — no whole-pipeline force needed here.
    try:
        from aiforge_core.config import agent_config as _acfg
        _doer_base = (_acfg.get("doer") or {}).get("base_url") or ""
    except Exception:  # noqa: BLE001
        _doer_base = ""
    _doer_is_loopback = (
        not _doer_base
        or "127.0.0.1:1234" in _doer_base
        or "localhost:1234" in _doer_base
    )
    if (_doer_is_loopback
            and os.environ.get("AIFORGE_LM_HEALTH", "1") in {"1", "true"}):
        try:
            from aiforge_core.runtime.lm_health import check_lm_health
            health = check_lm_health(restart_on_fail=True)
            if not health.get("doer_ok"):
                log.warning(
                    "ticket=%s local LM unreachable — pipeline relies on the "
                    "configured retry chain (restarted=%s)",
                    ticket.identifier, health.get("restarted"))
        except Exception as exc:  # noqa: BLE001
            log.debug("lm_health probe skipped: %s", exc)

    worktree, prior_env = _setup_ticket_workspace(ticket)
    if not worktree:
        tickets_mod.update_status(
            ticket.id, "blocked", role="adk_runner",
            metadata_patch={
                "error": (
                    f"no target repo for project={ticket.project!r}; "
                    "set ticket.project to a directory under "
                    f"AIFORGE_WORKTREE_ROOT ({os.environ.get('AIFORGE_WORKTREE_ROOT', '~/codeRepo')})"
                )[:500],
            },
        )
        _restore_env(prior_env)
        return True

    # Pull operator-uploaded files into the per-ticket worktree so the
    # Doer can ``file_read`` them by the same relative path the Doer
    # prompt references.
    _materialize_attachments_in_worktree(ticket, worktree)

    # Gap-10 wire-in: persist any image attachments as an
    # Observation_v2 with ``media_refs`` so future tickets can recall
    # "this ticket had screenshots X / Y" — even before the vision
    # embedder lands. Soft-fails on any backend error.
    _persist_ticket_media(ticket)

    # C5: spec → failing-test scaffold. Parses ticket body's
    # "Acceptance" bullets and writes a per-language test file under
    # ``tests/aiforge_spec/``. Doer's run_tests then has a TDD target.
    if os.environ.get("AIFORGE_SPEC_TO_TESTS", "1") in {"1", "true"}:
        try:
            from aiforge_core.runtime.spec_to_tests import write_scaffold
            md = ticket.metadata or {}
            language = md.get("test_language", "python")
            write_scaffold(
                ticket.identifier, ticket.body or "",
                repo_root=worktree, language=language,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("spec_to_tests skipped: %s", exc)

    # Gap-9 wire-in: when the ticket metadata lists external references
    # (Confluence / Slack thread / Jira ticket / plain URLs), pull them
    # into AFM via the external_ingest spine so the Doer's memory_block
    # hit list can include their content. ``external_refs`` is a list
    # of strings (URLs / paths) on ticket.metadata. Skipped silently
    # when absent. Soft-fail on any backend error.
    _ingest_ticket_external_refs(ticket)

    memory_md = memory_block.fetch(ticket)
    # Enhancer + Validator now run as proper ADK LlmAgents inside the
    # SequentialAgent (see :func:`pipeline.build_pipeline`). The
    # enhanced body lands in ``state['enhanced_body']`` for the
    # Planner/Doer; the validator's verdict lands in
    # ``state['validator_verdict']`` for the runner to fold into
    # ticket metadata.
    prompt = _build_prompt(ticket, memory_md)

    forced = _ticket_force_provider(ticket)
    set_force_provider(forced)
    if forced:
        log.info("ticket=%s force_provider=%s", ticket.identifier, forced)

    # Deploy autonomy — when the operator chose 'qa' or 'prod' at
    # ticket-creation time, the deploy recipe will merge the PR + wait
    # for the new SHA. We arm both env knobs in the runner's process
    # so the live_verifier's bash commands see them. Reset to the
    # prior values in the ``finally`` cleanup below (``_restore_env``).
    deploy_target = ((ticket.metadata or {}).get("deploy_target")
                     or "none").lower()
    if deploy_target in {"qa", "prod"}:
        os.environ["AIFORGE_AUTO_MERGE"] = "1"
        os.environ["AIFORGE_DEPLOY_TARGET"] = deploy_target
        log.info(
            "ticket=%s deploy_target=%s (auto-merge armed)",
            ticket.identifier, deploy_target,
        )
    else:
        # Belt-and-braces: a previous run with deploy_target=qa MUST
        # NOT leak its auto-merge env into the next claim.
        os.environ.pop("AIFORGE_AUTO_MERGE", None)
        os.environ.pop("AIFORGE_DEPLOY_TARGET", None)

    # Researcher routing: skip the read-only context gatherer on
    # greenfield tickets where the body has no reference patterns AND
    # the repo's git log doesn't mention the project keyword. Saves
    # ~5 LM calls + ~4min on tickets where Researcher would find
    # nothing relevant. ``AIFORGE_RESEARCHER_FORCE=1`` overrides.
    skip_researcher, skip_reason = should_skip_researcher(
        ticket.title or "", ticket.body or "",
    )
    log.info("ticket=%s researcher=%s reason=%s",
             ticket.identifier,
             "skip" if skip_researcher else "run", skip_reason)

    try:
        state = asyncio.run(_run_pipeline(
            prompt, skip_researcher=skip_researcher, ticket=ticket,
            memory_md=memory_md,
        ))
        outcome = _extract_verdict(state)
        # Capture the Feedback rationale BEFORE any mutation so an
        # operator scanning ticket_events sees both the verdict and
        # the convergence reason. Best-effort: any persistence error
        # is logged + swallowed inside the helper so the runner still
        # makes forward progress on the ticket itself.
        reason = _extract_reason(state, outcome)
        _record_verdict_event(ticket.id, outcome, reason)

        # live_verifier no longer runs inside the pipeline — it runs
        # standalone AFTER the PR opens (see below) so its deploy recipe
        # has a real PR_URL to merge. ``lv`` is populated there.
        lv: dict | None = None

        new_status = _VERDICT_TO_STATUS.get(outcome, "blocked")

        # Validator (ADK LlmAgent) wrote validator_verdict into the
        # session state; surface it on the ticket so operators see the
        # final pre-PR verdict.
        validator_out: Any = state.get("validator_verdict") if state else None
        if isinstance(validator_out, str):
            try:
                import json as _json
                validator_out = _json.loads(validator_out)
            except Exception:
                validator_out = {"raw": validator_out[:400]}

        # The Doer (running on the operator's configured model + retry
        # chain) is the only path that finishes a ticket now — kept for
        # ops-dashboard / training provenance.
        handled_by = "pipeline"

        # PR gate: anything that ISN'T an explicit scope_violation is
        # eligible. `commit_push_open_pr` itself short-circuits on a
        # clean tree, so verdict=fail with no edits stays a no-op.
        pr_meta: dict[str, Any] = {}
        if outcome != "scope_violation":
            pr_meta = commit_push_open_pr(ticket)

        # Empty-production-diff: git_pr rejected because the Doer only
        # wrote tests / fixtures with no edit to ``src/main``. Demote
        # the verdict so the ticket lands as ``blocked`` (not ``done``)
        # and the operator sees a clear reason instead of an empty PR.
        if pr_meta.get("pr_skip_reason") == "test_only_diff":
            outcome = "fail"
            new_status = "blocked"
            log.warning(
                "ticket=%s test_only_diff — demoting verdict to fail. "
                "Doer wrote: %s",
                ticket.identifier,
                ", ".join(pr_meta.get("test_only_files", [])[:5]),
            )

        # Live verifier — runs HERE (post-PR) so its deploy recipe has a
        # real PR_URL to merge + roll out before testing. Only when the
        # PR actually opened and the verdict is otherwise a pass. A
        # failing live verify flips the ticket to blocked so the operator
        # knows the merged/worktree fix didn't actually hold.
        if (
            pr_meta.get("pr_url")
            and outcome == "pass"
            and os.environ.get("AIFORGE_LIVE_VERIFIER", "1") in {"1", "true"}
        ):
            try:
                lv = _run_live_verifier(ticket, pr_meta["pr_url"])
            except Exception as exc:  # noqa: BLE001
                log.warning("live_verifier standalone failed: %s", exc)
                lv = None
            if lv is not None and lv.get("ok") is False:
                outcome = "fail"
                new_status = "blocked"
                reason = f"live_verifier rejected: {(lv.get('rationale') or '')[:300]}"
                log.warning(
                    "ticket=%s live_verifier ok=false: %s",
                    ticket.identifier, (lv.get("rationale") or "")[:200],
                )
            elif lv is not None and lv.get("ok") is True and os.environ.get(
                "AIFORGE_AUTO_MERGE_ON_VALIDATE", "1"
            ) in {"1", "true"}:
                # live_verifier validated the behaviour → merge the PR.
                # For deploy_target=qa/prod the deploy recipe already
                # merged it, so merge_pr reports already_merged — still
                # surfaced so the operator sees the final state.
                try:
                    from .git_pr import merge_pr
                    merge_meta = merge_pr(pr_meta["pr_url"])
                    pr_meta["pr_merged"] = merge_meta.get("merged")
                    pr_meta["pr_merge_reason"] = merge_meta.get("reason")
                    log.info(
                        "ticket=%s auto-merge merged=%s reason=%s",
                        ticket.identifier, merge_meta.get("merged"),
                        merge_meta.get("reason"),
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("auto-merge failed: %s", exc)

        # C1: grade the PR's CI runs once the push is in. Empty PR
        # metadata (no diff to ship) skips this. Soft-fail: any
        # ``gh`` error lands in pr_meta as ``ci_*`` keys for an
        # operator to inspect, never blocks status update.
        ci_meta: dict[str, Any] = {}
        if pr_meta.get("pr_url") and os.environ.get(
            "AIFORGE_CI_GRADE", "1"
        ) in {"1", "true"}:
            try:
                from aiforge_core.runtime.ci_feedback import grade_and_react
                ci_meta = grade_and_react(
                    pr_meta["pr_url"],
                    poll_seconds=int(
                        os.environ.get("AIFORGE_CI_POLL_S", "30"),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                ci_meta = {"ok": False, "error": str(exc)[:200]}

        # C2: second-agent PR review pass. Posts a structured verdict
        # comment so a human (or follow-up agent) can react. Soft-fail.
        review_meta: dict[str, Any] = {}
        if pr_meta.get("pr_url"):
            try:
                from aiforge_core.runtime.pr_reviewer import review_pr
                review_meta = review_pr(
                    pr_meta["pr_url"],
                    ticket.title or "",
                    ticket.body or "",
                )
            except Exception as exc:  # noqa: BLE001
                review_meta = {"ok": False, "error": str(exc)[:200]}

        tickets_mod.update_status(
            ticket.id, new_status, role="adk_runner",
            metadata_patch={
                "feedback_verdict": outcome,
                "verifier_verdict": _extract_verifier(state),
                **pr_meta,
                **({"ci_status": ci_meta.get("status"),
                    "ci_rolled_back": ci_meta.get("rolled_back", False),
                    "ci_checks": ci_meta.get("checks") or []}
                   if ci_meta else {}),
                **({"review_verdict": review_meta.get("verdict"),
                    "review_axes": review_meta.get("axes") or {}}
                   if review_meta and review_meta.get("ok") else {}),
                **({"validator_verdict": (validator_out or {}).get("verdict"),
                    "validator_rationale": (validator_out or {}).get("rationale"),
                    "validator_scope_ok": (validator_out or {}).get("scope_ok"),
                    "validator_regression_risk":
                        (validator_out or {}).get("regression_risk")}
                   if validator_out else {}),
                # Provenance: which path finished the ticket. "pipeline" =
                # the configured-model pipeline (+ retry chain) cleared it.
                "handled_by": handled_by,
                **({
                    "live_verifier_ok": (lv or {}).get("ok"),
                    "live_verifier_rationale": (lv or {}).get("rationale"),
                    "live_handoff": (lv or {}).get("live_handoff", False),
                    "handoff_brief": (lv or {}).get("handoff_brief"),
                } if lv else {}),
            },
        )
        log.info("ticket=%s status=%s verdict=%s",
                 ticket.identifier, new_status, outcome)

    except Exception as exc:
        # The concrete cause is already surfaced concisely upstream
        # (EscalatingLlm's ``llm.exhausted`` / ``llm.attempt_failed`` lines).
        # A full chained traceback here is redundant noise for the common
        # case (flaky/down model). Log a meaningful one-liner; restore the
        # raw stack with AIFORGE_ADK_TRACEBACKS=1 for novel failures.
        if str(os.environ.get("AIFORGE_ADK_TRACEBACKS", "")).strip().lower() in (
                "1", "true", "yes", "on"):
            log.exception("ticket=%s failed during ADK run: %s",
                          ticket.identifier, exc)
        else:
            log.error("ticket=%s failed during ADK run: %s: %s",
                      ticket.identifier, type(exc).__name__, str(exc)[:400])
        # Even on ADK failure, the Doer may have written real files
        # before the orchestrator stalled. Surface that work as a draft PR for
        # human review instead of dropping it. The function
        # short-circuits with pr_skip_reason=no_changes on a clean tree.
        rescue_meta: dict[str, Any] = {}
        try:
            rescue_meta = commit_push_open_pr(ticket)
            if rescue_meta.get("pr_url"):
                log.info(
                    "ticket=%s rescued partial work as PR despite "
                    "ADK failure: %s", ticket.identifier,
                    rescue_meta["pr_url"],
                )
        except Exception as rescue_exc:
            log.warning("ticket=%s PR rescue also failed: %s",
                        ticket.identifier, rescue_exc)
        try:
            tickets_mod.update_status(
                ticket.id, "blocked", role="adk_runner",
                metadata_patch={"error": str(exc)[:500], **rescue_meta},
            )
        except Exception:
            pass
    finally:
        # Always clear the per-ticket override so the next claim builds
        # against the operator's profile, not the previous ticket's
        # forced provider.
        set_force_provider(None)
        _restore_env(prior_env)
    return True


def main() -> int:
    """Single-shot: claim one ticket, run it, exit."""
    if _process_one_ticket():
        return 0
    time.sleep(int(os.environ.get("AIFORGE_POLL_IDLE_S", "10")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
