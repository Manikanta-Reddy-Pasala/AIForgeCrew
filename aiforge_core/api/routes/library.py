"""Playbook Library routes — skills · workflows · rules (+ the public workflow
registry view). Extracted from api.py (behavior-preserving). Operator-managed
instruction library: list all, create from text or via the LLM, delete/clear.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()


@router.get("/api/workflows")
def list_workflows() -> list[dict]:
    """Public registry view — UI uses this to populate the workflow
    dropdown on the new-ticket form."""
    from aiforge_core.workflows import list_all
    return [w.to_public_dict() for w in list_all()]


def _bundled_names(kind: str) -> set:
    """Filenames of the BUNDLED default playbooks for a kind. These ship in
    ``runtime/builtin_playbooks/{kind}`` and ``ensure_dirs()`` COPIES them into
    the user-writable global dir (keeping the filename), so a path check can't
    tell a seeded default from a user file — but the FILENAME still identifies
    it. Cached per process."""
    cache = _bundled_names.__dict__.setdefault("_cache", {})
    if kind not in cache:
        try:
            from pathlib import Path

            from aiforge_core.runtime import workflows as _wf
            d = Path(_wf.__file__).resolve().parent / "builtin_playbooks" / kind
            cache[kind] = {f.name for f in d.glob("*.md")} if d.is_dir() else set()
        except Exception:  # noqa: BLE001
            cache[kind] = set()
    return cache[kind]


def _library_origin(source: str, kind: str) -> str:
    """``"default"`` when the item is one of the bundled playbooks (matched by
    its source FILENAME), else ``"custom"`` — everything the user or a repo
    added. Never raises (classification must not break the listing)."""
    try:
        from pathlib import Path
        if source and Path(source).name in _bundled_names(kind):
            return "default"
    except Exception:  # noqa: BLE001
        return "custom"
    return "custom"


def _skill_dict(s, kind: str | None = None) -> dict:
    source = getattr(s, "source", "")
    return {"name": s.name, "description": s.description,
            "triggers": list(getattr(s, "triggers", []) or []),
            "body": s.body, "source": source,
            "always": bool(getattr(s, "always", False)),
            "origin": _library_origin(source, kind) if kind else "default"}


@router.get("/api/library/{kind}")
def library_list(kind: str) -> list[dict]:
    """List all skills / workflows / rules."""
    if kind == "skills":
        from aiforge_core.runtime import skills
        return [_skill_dict(s, "skills") for s in skills.load()]
    if kind == "workflows":
        from aiforge_core.runtime import workflows
        return [_skill_dict(w, "workflows") for w in workflows.load()]
    if kind == "rules":
        from aiforge_core.runtime import repo_rules
        return [{"name": r.name, "description": r.description,
                 "triggers": list(r.triggers), "scope": r.scope,
                 "body": r.body, "source": r.source,
                 "globs": list(r.globs), "always": r.always,
                 "origin": _library_origin(r.source, "rules")}
                for r in repo_rules.load_global_and_builtin()]
    raise HTTPException(404, f"unknown kind {kind!r}")


@router.post("/api/library/{kind}", status_code=201)
def library_create(kind: str, payload: dict = Body(...)) -> dict:
    """Create/overwrite a skill / workflow / rule from text."""
    name = (payload.get("name") or "").strip()
    body = (payload.get("body") or "").strip()
    desc = (payload.get("description") or "").strip()
    triggers = payload.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [t.strip() for t in triggers.split(",") if t.strip()]
    if not name or not body:
        raise HTTPException(400, "name and body are required")
    if kind == "skills":
        from aiforge_core.runtime import skills
        res = skills.write_skill(name, desc, body, triggers)
    elif kind == "workflows":
        from aiforge_core.runtime import workflows
        res = workflows.write_workflow(name, desc, body, triggers)
    elif kind == "rules":
        from aiforge_core.runtime import repo_rules
        res = repo_rules.write_rule(name, body, globs=payload.get("globs"),
                                    always=bool(payload.get("always", True)))
    else:
        raise HTTPException(404, f"unknown kind {kind!r}")
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "write failed"))
    return res


@router.delete("/api/library/{kind}/{name}")
def library_delete(kind: str, name: str) -> dict:
    """Delete a single skill / workflow / rule by name (custom or default)."""
    if kind == "skills":
        from aiforge_core.runtime import skills
        res = skills.delete_skill(name)
    elif kind == "workflows":
        from aiforge_core.runtime import workflows
        res = workflows.delete_workflow(name)
    elif kind == "rules":
        from aiforge_core.runtime import repo_rules
        res = repo_rules.delete_rule(name)
    else:
        raise HTTPException(404, f"unknown kind {kind!r}")
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "delete failed"))
    return res


@router.delete("/api/library/{kind}")
def library_clear(kind: str) -> dict:
    """Clear ALL skills / workflows / rules of a kind (custom + defaults)."""
    if kind == "skills":
        from aiforge_core.runtime import skills
        return skills.clear_skills()
    if kind == "workflows":
        from aiforge_core.runtime import workflows
        return workflows.clear_workflows()
    if kind == "rules":
        from aiforge_core.runtime import repo_rules
        return repo_rules.clear_rules()
    raise HTTPException(404, f"unknown kind {kind!r}")


_LIBRARY_GEN_PROMPT = {
    "skills": ("Write a SKILL.md. Output ONLY a markdown doc with YAML "
               "frontmatter (name, description, triggers: [..]) then a concise "
               "instruction body the agent follows. Topic: "),
    "workflows": ("Write a WORKFLOW.md: YAML frontmatter (name, description, "
                  "triggers: [..]) then numbered end-to-end steps. Topic: "),
    "rules": ("Write a coding RULE as a short markdown doc: one '# Title' then "
              "tight imperative bullet points the agent must follow. Topic: "),
}


@router.post("/api/library/{kind}/generate")
def library_generate(kind: str, payload: dict = Body(...)) -> dict:
    """Draft a skill / workflow / rule from a text description using the
    configured LLM. Returns the draft markdown for review before saving."""
    if kind not in _LIBRARY_GEN_PROMPT:
        raise HTTPException(404, f"unknown kind {kind!r}")
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    role = payload.get("role") or "architect"
    try:
        from aiforge_core.llm import client
        draft = client.complete(role, [
            {"role": "system", "content": "You author concise, high-signal "
             "agent instruction docs. Output ONLY the markdown, no preamble."},
            {"role": "user", "content": _LIBRARY_GEN_PROMPT[kind] + prompt},
        ], max_tokens=1200)
    except Exception as exc:  # noqa: BLE001 — surface model/credit errors
        raise HTTPException(502, f"LLM generate failed: {exc}")
    return {"ok": True, "draft": draft}


__all__ = ["router"]
