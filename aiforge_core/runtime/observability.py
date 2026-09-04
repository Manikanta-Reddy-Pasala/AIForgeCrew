"""Persist per-stage pipeline events to ``ticket_events`` for the UI.

The runner already records ``status_change`` rows (in_progress / blocked
/ done) and one ``verdict_attempt`` row (PR #21). What's missing for a
useful audit timeline is the in-between: which archetype is running,
when it starts, when it ends, when a Doer commit lands, when a PR is
opened. Without these, the UI just shows ``in_progress`` for an hour
and the operator can't tell if the Doer is editing files, if the
Verifier rejected, or if the model stalled mid-turn.

This module exposes helpers that emit:

  - ``stage_start``   — agent_role={archetype}, body=instruction excerpt
  - ``stage_done``    — same, body=output excerpt
  - ``commit``        — Doer-self-committed via PR #22 git_commit tool
  - ``pr_opened``     — git_pr.commit_push_open_pr returned a pr_url

All writes are best-effort: a Postgres hiccup logs a warning but never
breaks the agent loop. The events table indices already cover the
high-cardinality columns (``ticket_id, created_at``, ``kind``).
"""
from __future__ import annotations

import logging
import os
from typing import Any


log = logging.getLogger("aiforge.observability")


_BODY_MAX_CHARS = 600


def _is_disabled() -> bool:
    return os.environ.get("AIFORGE_OBSERVABILITY_DISABLE", "0") in ("1", "true")


def _trim(text: str | None) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    if len(text) > _BODY_MAX_CHARS:
        text = text[: _BODY_MAX_CHARS - 1].rstrip() + "…"
    return text


def _ticket_id_from_state(state) -> int | None:
    """Resolve the Postgres ticket.id from session state.

    Pipeline seeds ``state['ticket_identifier']`` (e.g. ``ONE-1``) at
    runner start; we look up the row's bigint ``id`` because that's
    what ``ticket_events.ticket_id`` references."""
    try:
        identifier = state.get("ticket_identifier")
    except AttributeError:
        return None
    if not identifier:
        return None
    try:
        from aiforge_core.tickets import store as tickets_mod
        ticket = tickets_mod.get(identifier)
        if ticket is None:
            return None
        return getattr(ticket, "id", None)
    except Exception as exc:  # noqa: BLE001
        log.debug("observability.lookup_failed: %s", exc)
        return None


def _emit(
    *, ticket_id: int, agent_role: str, kind: str,
    body: str = "", metadata: dict[str, Any] | None = None,
) -> None:
    if _is_disabled():
        return
    try:
        from aiforge_core.tickets import store as tickets_mod
        tickets_mod.add_event(
            ticket_id, agent_role, kind, _trim(body), metadata or {},
        )
    except Exception as exc:  # noqa: BLE001 — best-effort audit
        log.warning(
            "observability.emit_failed kind=%s role=%s: %s",
            kind, agent_role, exc,
        )


def _resolve_stage_attribution(role: str) -> dict[str, Any]:
    """Compute which model + provider the named role actually ran under.

    Reads ``agents.yaml`` for the configured pair and folds in any
    pipeline-wide ``force_provider`` override. Returns a dict with
    ``model_configured``, ``provider_configured``, ``force_provider``,
    and ``effective_provider`` so the UI can render a per-stage badge
    without re-deriving the override logic.
    """
    out: dict[str, Any] = {}
    try:
        from aiforge_core.config.agent_config import get as get_role_cfg
        cfg = get_role_cfg(role) or {}
        model = cfg.get("model")
        # LM Studio paths are absolute filesystem locations — show the
        # leaf (model directory name) so the UI chip stays readable.
        if isinstance(model, str) and "/" in model:
            model = model.rsplit("/", 1)[-1] or model
        out["model_configured"] = model
        out["provider_configured"] = cfg.get("provider")
    except Exception:
        pass
    try:
        from aiforge_core.runtime.pipeline import get_force_provider
        forced = get_force_provider()
        if forced:
            out["force_provider"] = forced
    except Exception:
        pass
    out["effective_provider"] = (
        out.get("force_provider")
        or out.get("provider_configured")
        or "unknown"
    )
    return out


_STAGE_OUTPUT_KEYS = ("{role}_outcome", "{role}_verdict", "{role}_plan",
                      "facts_json", "feedback_verdict", "verifier_verdict")


def _stage_output(role: str, state) -> str:
    """The role's produced text — its ``{role}_outcome`` slot, then common
    fallback output keys."""
    for tmpl in _STAGE_OUTPUT_KEYS:
        v = state.get(tmpl.format(role=role))
        if v:
            return str(v)
    return ""


def make_stage_callbacks(role: str) -> tuple:
    """Return ``(before, after)`` ADK callbacks that emit ``stage_start``
    / ``stage_done`` events for ``role``. Wire onto every LlmAgent.

    Both events carry per-stage model attribution in ``metadata`` so the UI can
    show "Enhancer → qwen3-coder-next (local)" instead of leaving the operator
    to guess.
    """
    if _is_disabled():
        return (None, None)

    def _before(*, callback_context, **_kw):
        try:
            tid = _ticket_id_from_state(callback_context.state)
            if tid is None:
                return None
            _emit(ticket_id=tid, agent_role=role, kind="stage_start",
                  body=f"{role} entered",
                  metadata={"role": role, "phase": "start",
                            **_resolve_stage_attribution(role)})
        except Exception as exc:  # noqa: BLE001
            log.debug("stage_start.failed role=%s: %s", role, exc)
        return None

    def _after(*, callback_context, **_kw):
        try:
            state = callback_context.state
            tid = _ticket_id_from_state(state)
            if tid is None:
                return None
            output = _stage_output(role, state)
            _emit(ticket_id=tid, agent_role=role, kind="stage_done",
                  body=f"{role} produced: {output}" if output else f"{role} done",
                  metadata={"role": role, "phase": "done",
                            **_resolve_stage_attribution(role)})
        except Exception as exc:  # noqa: BLE001
            log.debug("stage_done.failed role=%s: %s", role, exc)
        return None

    return (_before, _after)


def emit_pr_opened(*, ticket_id: int, pr_url: str,
                   branch: str = "", commits: int = 0) -> None:
    """Persist a ``pr_opened`` event with the PR url. Called from
    git_pr.commit_push_open_pr after the gh CLI returns a url."""
    if not pr_url:
        return
    _emit(
        ticket_id=ticket_id, agent_role="git_pr", kind="pr_opened",
        body=pr_url,
        metadata={"pr_url": pr_url, "branch": branch, "commits": commits},
    )


def emit_context_injected(
    *, ticket_id: int, agent_role: str = "pipeline",
    skills: list[dict] | None = None,
    rules: list[dict] | None = None,
    workflows: list[dict] | None = None,
) -> None:
    """Persist a ``context_injected`` event recording WHICH skills / rules /
    workflows a run pulled into the agents' context, and HOW each was chosen
    (``why`` = always / match for skills+workflows; rules carry ``source``).

    This is the data the Workflow UI overlays onto its nodes so an operator
    can read the graph and see exactly what extra knowledge each stage used.
    No-op when nothing was injected."""
    skills = skills or []
    rules = rules or []
    workflows = workflows or []
    if not (skills or rules or workflows):
        return
    _emit(
        ticket_id=ticket_id, agent_role=agent_role, kind="context_injected",
        body=(f"injected: {len(skills)} skill(s), {len(rules)} rule(s), "
              f"{len(workflows)} workflow(s)"),
        metadata={"skills": skills, "rules": rules, "workflows": workflows},
    )


def emit_commit(*, ticket_id: int, sha: str, message: str,
                role: str = "doer") -> None:
    """Persist a ``commit`` event when Doer fires the ``git_commit``
    tool (PR #22). Called from doer_tools.git_commit on success."""
    if not sha:
        return
    _emit(
        ticket_id=ticket_id, agent_role=role, kind="commit",
        body=f"{sha[:8]} {message}",
        metadata={"sha": sha, "message": message},
    )


__all__ = [
    "make_stage_callbacks",
    "emit_pr_opened",
    "emit_commit",
    "emit_context_injected",
]
