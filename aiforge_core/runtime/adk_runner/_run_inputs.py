"""What a pipeline run is given: repo rules, toolchain and user preferences,
ticket state, images and the run config."""
from __future__ import annotations

import os
from typing import Any

from ._base import log, tickets_mod
from ._context import (
    _run_repo_root,
)


def _pkg():
    """``_pipeline``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``_pipeline``; patch any other
    name on this module."""
    import aiforge_core.runtime.adk_runner._pipeline as package
    return package


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


def _glob_list(raw) -> list[str]:
    """A glob allowlist, from a newline string or an already-parsed list."""
    if isinstance(raw, str):
        raw = [g.strip() for g in raw.splitlines() if g.strip()]
    return [str(g) for g in raw if g] if isinstance(raw, list) else []


def _collect_repo_rules(ticket, scope_seed: list) -> str:
    """Glob-scoped repo rules (Cursor-style), collected BEFORE the build so a
    repo that carries rules files skips the paid ctx_conventions LLM branch
    entirely — the rules ARE the conventions, for free."""
    try:
        from aiforge_core.runtime import repo_rules
        query = ""
        if ticket is not None:
            query = (f"{getattr(ticket, 'title', '') or ''}\n"
                     f"{getattr(ticket, 'body', '') or ''}")
        rules_md, ambiguous = repo_rules.collect_or_ask(
            _run_repo_root(), scope_seed, query)
        if ticket is not None:
            _pkg()._emit_ambiguous_rule_notice(ticket, ambiguous)
        return rules_md
    except Exception as exc:  # noqa: BLE001
        log.debug("repo_rules collect failed: %s", exc)
        return ""


def _emit_rules_injected(ticket, scope_seed: list) -> None:
    """Workflow-transparency: record which repo rules applied to this ticket's
    scope so the Workflow UI can surface them."""
    try:
        from aiforge_core.runtime import observability as _obs
        from aiforge_core.runtime import repo_rules
        names = repo_rules.matched_names(
            _run_repo_root(), scope_seed)
        tid = getattr(ticket, "id", None)
        if tid is not None and names:
            _obs.emit_context_injected(ticket_id=tid, agent_role="pipeline",
                                       rules=names)
    except Exception as exc:  # noqa: BLE001
        log.debug("context_injected.emit (rules) failed: %s", exc)


def _toolchain_md() -> str:
    """Host-verified toolchain (python3 vs python, ./mvnw vs mvn, …) so the Doer
    uses the right commands instead of re-discovering them by trial-and-error
    every ticket. Cheap + cached (shutil.which); never blocks a run."""
    try:
        from aiforge_core.config import repo_standards as _rstd
        from aiforge_core.runtime.sandbox import root as _root
        return _rstd.toolchain_brief(str(_root())) or ""
    except Exception:  # noqa: BLE001
        return ""


def _user_prefs_md() -> str:
    """Durable user preferences (gap #9) — global, cross-repo, so the agent
    honours "I always want X" without being re-told. Sourced from the embedded
    sqlite ``pref:`` units chat_capture writes.
    """
    parts = []
    try:
        from aiforge_core.runtime import user_prefs as _up
        block = _up.preferences_block()
        if block:
            parts.append(block)
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime.chat_agent import _preferences_context
        block = _preferences_context(_run_repo_root() or ".")
        if block:
            parts.append(block)
    except Exception:  # noqa: BLE001
        pass
    return "\n\n".join(parts)


def seed_gaming_base(state: dict) -> dict:
    """The tree before the run (a user's uncommitted work in a pinned repo):
    the test-gaming check judges only the lines THIS run adds."""
    try:
        from aiforge_core.runtime.gaming_changes import baseline
        from aiforge_core.runtime.graph_pipeline._scope import _repo_root_for_scope
        base = baseline(_repo_root_for_scope())
        if base:
            state["gaming_base"] = base
    except Exception:  # noqa: BLE001 — the check falls back to HEAD
        pass
    return state


def _ticket_state(ticket, scope_seed: list, rules_md: str,
                  memory_md: str) -> dict:
    """The session state seeded from the ticket."""
    pkg = _pkg()
    state: dict[str, Any] = {
        "ticket_identifier": getattr(ticket, "identifier", "") or "",
        "ticket_project": getattr(ticket, "project", "") or "",
        "ticket_title": getattr(ticket, "title", "") or "",
        # RAW ASK for the enhancer degenerate-output guard (pipeline.py): the
        # guard compares state['enhanced_body'] against this and restores it
        # when the rewrite collapsed / dropped every named anchor.
        "raw_ask": ((getattr(ticket, "title", "") or "") + "\n"
                    + (getattr(ticket, "body", "") or "")).strip(),
    }
    # C6 scope enforcement: the UI stores the operator's allowlist in
    # ticket.metadata. Without this seed, scope_guard / verify_scope / the
    # Validator's rule 2 all judged a permanently-empty field.
    clean = _glob_list((getattr(ticket, "metadata", None) or {})
                       .get("scope_allowlist_globs"))
    if clean:
        state["scope_allowlist_globs"] = clean
        # Durable copy for plan_promote: replans clear the live key
        # (plan-derived globs are per-plan) but the operator's seed must
        # survive every epoch.
        state["scope_allowlist_globs_seeded"] = list(clean)
    if rules_md:
        # plan_promote re-matches once the plan widens the globs. Injected via
        # {rules_md?} in prompts.
        state["rules_md"] = rules_md
        pkg._emit_rules_injected(ticket, scope_seed)
    # Pre-flight memory recall — seeded as STATE, not stitched into the seed
    # prompt: ONE {memory_brief_md?} instruction copy per consuming agent
    # (enhancer/planner/doer/verify_risk) instead of 60-120 history replays.
    # Also replaces the ctx_memory LLM agent, which re-queried the same
    # backends.
    if memory_md:
        state["memory_brief_md"] = memory_md
    for key, value in (("toolchain_md", pkg._toolchain_md()),
                       ("user_prefs_md", pkg._user_prefs_md())):
        if value:
            state[key] = value
    return state


def _with_images(content, ticket, _gtypes):
    """Sub #6 follow-up: inject multimodal image parts when the ticket has image
    attachments AND the Doer model supports vision."""
    try:
        from aiforge_core.config.agent_config import load_all as get_config
        from aiforge_core.runtime.vision_adk import inject_image_parts
        doer_model = (get_config().get("doer", {}) or {}).get("model", "")
        images = [str(f.get("path", ""))
                  for f in ((ticket.metadata or {}).get("attached_files") or [])
                  if isinstance(f, dict) and f.get("path")
                  and str(f.get("name", "")).lower().endswith(
                      (".png", ".jpg", ".jpeg", ".gif", ".webp"))]
        if not images:
            return content
        injected = inject_image_parts([content], doer_model, images)
        return injected[0] if injected and injected[0] is not content else content
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("vision_adk.inject failed: %s", exc)
        return content


def _pipeline_run_config():
    """Hard ceiling on total LLM calls for the whole pipeline run.

    A local model (Qwen) can thrash — ONE-7 made 383 calls across 52 minutes and
    wrote ZERO files, spinning on read/think without ever committing an edit.
    ADK's default cap is high enough that it never tripped. Bounding it means a
    stuck local Doer aborts (and lands the ticket as blocked) instead of burning
    an hour. The v6 Workflow graph is wider than the old Sequential pipeline —
    triage + 4 context branches + 3 verifiers + the Doer loop (≤3×) + a possible
    verifier-replan AND validator-replan each re-running planner/verify/doer. A
    healthy full+replan run can use ~120-160 calls, so the old 120 ceiling
    tripped mid-Doer exactly on the harder tickets. Tune via
    AIFORGE_MAX_LLM_CALLS.
    """
    try:
        from google.adk.agents.run_config import RunConfig
        return RunConfig(
            max_llm_calls=int(os.environ.get("AIFORGE_MAX_LLM_CALLS", "600")))
    except Exception as exc:  # noqa: BLE001
        log.debug("RunConfig unavailable: %s", exc)
        return None
