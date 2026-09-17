"""The seed prompt for a ticket: operator comments, attachments, the playbook,
vision notes, and the ticket's own provider and external references."""
from __future__ import annotations

import os

from ._base import log, tickets_mod


def _pkg():
    """The parent module, looked up on each call so a name patched there is the
    one used here."""
    import aiforge_core.runtime.adk_runner._orchestrate as package
    return package


def _operator_comments_block(ticket) -> str:
    """Operator follow-up comments: anything the human added via
    POST /api/tickets/{id}/comments after ticket creation.

    The Enhancer would otherwise never see this signal — it only reads
    ``ticket.body``. Folded in chronological order; bot/agent comments are
    excluded so the Doer doesn't loop on its own past commentary.
    """
    try:
        evts = tickets_mod.comments(ticket.id) or []
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("comment_fold_failed: %s", exc)
        return ""
    human = [e for e in evts
             if e.get("kind") == "comment"
             and (e.get("agent_role") or "").lower() == "human"
             and (e.get("body") or "").strip()]
    if not human:
        return ""
    out = ("\n## Operator follow-up comments\n"
           "These were posted on the ticket AFTER it was opened. "
           "Treat them as authoritative extensions of the body.\n\n")
    for c in human:
        out += (f"- _{str(c.get('created_at') or '')[:19]}_:\n"
                f"  {(c.get('body') or '').strip()}\n")
    return out


def _attachments_block(ticket) -> str:
    """List attachment paths so the Doer can ``file_read`` them via its file
    tools. The files are materialized into the worktree (see
    _materialize_attachments_in_worktree); each entry is the workspace-relative
    path the API persisted."""
    files = (ticket.metadata or {}).get("attached_files") or []
    rows = [f for f in files if isinstance(f, dict) and f.get("path")]
    if not rows:
        return ""
    out = "\n## Attached files (read these BEFORE you start)\n"
    for f in rows:
        out += (f"- `{f.get('path', '')}` ({f.get('name', '')}, "
                f"{f.get('size', '?')} bytes)\n")
    return out + ("\nThese files were uploaded by the operator with the "
                  "ticket. Use `file_read` to load them — their context is "
                  "REQUIRED for the change.\n")


def _playbook_prefix(hay: str, repo_cwd) -> tuple[str, list, list]:
    """``(prefix, skill names, workflow names)``.

    Relevance-searches the skill registry (SKILL.md playbooks, incl. ones the
    Doer authored via learn_skill) + always-on repo skills, and the workflow
    registry, keyed on ticket title + body. Best-effort: parse failures
    swallowed.
    """
    prefix = ""
    used_skills: list = []
    used_workflows: list = []
    try:
        from aiforge_core.runtime import skills as _skills
        block = _skills.auto_context(hay, repo_cwd)
        if block:
            prefix = block + "\n\n"
            used_skills = _skills.selected_names(hay, repo_cwd)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("skills.inject failed: %s", exc)
    try:
        from aiforge_core.runtime import workflows as _workflows
        block = _workflows.auto_context(hay, repo_cwd)
        if block:
            prefix = block + "\n\n" + prefix
            used_workflows = _workflows.selected_names(hay, repo_cwd)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("workflows.inject failed: %s", exc)
    return prefix, used_skills, used_workflows


def _emit_context_injected(ticket, skills: list, workflows: list) -> None:
    """Workflow-transparency: record which skills/workflows this run pulled in,
    so the Workflow UI can show it on the graph. Never blocks."""
    try:
        from aiforge_core.runtime import observability as _obs
        tid = getattr(ticket, "id", None)
        if tid is not None and (skills or workflows):
            _obs.emit_context_injected(ticket_id=tid, agent_role="pipeline",
                                       skills=skills, workflows=workflows)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("context_injected.emit (skills/workflows) failed: %s", exc)


def _vision_block(ticket) -> str:
    """Vision attach hint (sub #6). When the ticket has image attachments AND
    the active Doer model supports vision, list them with a flag so the model
    knows it can request a multimodal turn. Actual content-block conversion
    lives in vision.attach_image; wiring it through ADK's LlmRequest.contents
    shape is a follow-up."""
    try:
        from aiforge_core.config.agent_config import load_all as get_config
        from aiforge_core.runtime.vision import supports_vision
        doer_model = (get_config().get("doer", {}) or {}).get("model", "")
        if not supports_vision(doer_model):
            return ""
        images = [f for f in ((ticket.metadata or {}).get("attached_files") or [])
                  if isinstance(f, dict)
                  and str(f.get("name", "")).lower().endswith(
                      (".png", ".jpg", ".jpeg", ".gif", ".webp"))]
        if not images:
            return ""
        out = ("\n## Multimodal images (vision-enabled model)\n"
               "These attachments are images. Call `vision.attach_image`\n"
               "to convert each into multimodal content blocks.\n")
        return out + "".join(f"- `{img.get('path','')}`\n" for img in images)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("vision.attach_hint failed: %s", exc)
        return ""


def _build_prompt(ticket, memory_md: str) -> str:
    """Compose the seed prompt for the SequentialAgent.

    NOTE: memory_md is NO LONGER appended to the seed. The seed is replayed in
    contents on every chat-mode LLM call (60-120×/ticket ≈ 40-80K tokens of pure
    memory-block repetition). The block now seeds state['memory_brief_md']
    instead → merged once into {context_brief_md?} / {memory_brief_md?}
    instruction injections, which also survive compaction. (Param retained for
    call-site compatibility; the runner routes it into initial_state instead.)
    """
    pkg = _pkg()
    _ = memory_md
    body = (f"# Ticket {ticket.identifier}\n"
            f"## Title\n{ticket.title}\n\n"
            f"## Body\n{ticket.body or '(no body)'}\n"
            + pkg._operator_comments_block(ticket)
            + pkg._attachments_block(ticket))
    # Pass the ticket's repo root so REPO-SCOPED skills/workflows (in
    # <repo>/.aiforge/…) load too, not just the global ones. cwd=None loaded
    # global-only, so a repo-specific playbook was silently ignored by the
    # pipeline. Falls back to None (global-only) when the worktree isn't set.
    repo_cwd = os.environ.get("AIFORGE_REPO_ROOT") or None
    hay = f"{ticket.title or ''} {ticket.body or ''}"
    prefix, used_skills, used_workflows = pkg._playbook_prefix(hay, repo_cwd)
    pkg._emit_context_injected(ticket, used_skills, used_workflows)
    return prefix + body + pkg._vision_block(ticket)


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


def _external_refs(ticket) -> list[str]:
    """The ticket's external refs. Empty when it has none or no target repo."""
    if not ticket.project:
        return []
    refs = (ticket.metadata or {}).get("external_refs") or []
    return [r for r in refs if isinstance(r, str) and r.strip()]


def _ingest_ticket_external_refs(ticket) -> None:
    """Gap-9 wire-in: feed ``ticket.metadata.external_refs`` (a list of URLs /
    paths) into memory so the Doer sees their content.

    The graph-backed external-ingest store was removed (SQLite-only build), so
    this is now a soft no-op. The egress gate is kept intact.

    Disable with ``AIFORGE_EXTERNAL_INGEST=0``.
    """
    # The egress gate stays HERE, at the call site that reaches the network —
    # tests/python/runtime/test_web_egress_gated.py reads this function's source
    # to prove the control exists where the egress happens.
    if os.environ.get("AIFORGE_EXTERNAL_INGEST", "1") in ("0", "false", ""):
        return
    refs = _pkg()._external_refs(ticket)
    if not refs:
        return
