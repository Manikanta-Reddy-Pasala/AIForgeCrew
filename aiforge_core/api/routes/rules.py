"""Captured-rules transparency + explicit gate-disable flag routes.
Extracted from api.py (behavior-preserving). A gate is NEVER disabled by the
classifier — only by an explicit, scoped, revocable user action here.
"""
from __future__ import annotations

import os

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


@router.get("/api/rules")
def list_captured_rules(repo: str | None = None,
                        session_id: int | None = None) -> dict:
    """Captured rules/memories/feedback for the transparency panel, grouped by
    scope. Optional ``repo`` / ``session_id`` filters."""
    from aiforge_core.runtime import rule_capture
    items = rule_capture.list_captured(repo=repo, session_id=session_id)
    by_scope: dict[str, list] = {}
    for it in items:
        by_scope.setdefault(it.get("scope") or "global", []).append(it)
    return {"items": items, "by_scope": by_scope}


class _RuleScopeBody(BaseModel):
    scope: str = Field(..., description="'global' | 'project' | 'session'")
    repo_root: str | None = Field(
        None, description="repo root so a →project rescope writes .aiforge/rules")


@router.put("/api/rules/{rule_id}/scope")
def rescope_captured_rule(rule_id: str, body: _RuleScopeBody) -> dict:
    """Re-file a captured item under a new scope (correcting a misclass). Any
    gate flag the rule enabled moves with it (and a deleted/undone one is
    revoked)."""
    from aiforge_core.runtime import rule_capture
    repo_root = body.repo_root or os.environ.get("AIFORGE_REPO_ROOT") or None
    return rule_capture.rescope(rule_id, body.scope, repo_root=repo_root)


@router.delete("/api/rules/{rule_id}")
def delete_captured_rule(rule_id: str) -> dict:
    """Undo a captured item — removes it from its store AND revokes any gate
    flag it enabled (so the approval gate is re-enabled)."""
    from aiforge_core.runtime import rule_capture
    return {"ok": rule_capture.undo(rule_id)}


@router.get("/api/rules/flags")
def list_gate_flags() -> dict:
    """Active gate-disable flags grouped by scope, for the Auto-approvals
    panel."""
    from aiforge_core.runtime import rule_capture
    return {"by_scope": rule_capture.list_flags()}


class _GateFlagBody(BaseModel):
    name: str = Field(..., description="'commit_auto_approve' | 'allow_delete'")
    scope: str = Field(..., description="'session' | 'project' (global needs confirm)")
    repo: str | None = None
    session_id: int | None = None
    rule_id: str | None = None
    allow_global: bool = False


@router.post("/api/rules/flags")
def set_gate_flag_ep(body: _GateFlagBody) -> dict:
    """EXPLICITLY enable a gate-disable flag for a scope (user-confirmed opt-in).
    Refuses global unless allow_global is set."""
    from aiforge_core.runtime import rule_capture
    return rule_capture.set_gate_flag(
        body.name, scope=body.scope, repo=body.repo,
        session_id=body.session_id, rule_id=body.rule_id,
        allow_global=body.allow_global)


@router.delete("/api/rules/flags/{name}")
def clear_gate_flag_ep(name: str, scope: str, repo: str | None = None,
                       session_id: int | None = None) -> dict:
    """Revoke a gate-disable flag for a scope (re-enables the gate)."""
    from aiforge_core.runtime import rule_capture
    ok = rule_capture.clear_gate_flag(name, scope=scope, repo=repo,
                                      session_id=session_id)
    return {"ok": ok}


__all__ = ["router"]
