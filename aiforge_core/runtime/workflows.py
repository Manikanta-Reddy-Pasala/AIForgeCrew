"""Workflow registry — ``WORKFLOW.md`` playbooks, searchable + self-authored.

A *workflow* is a multi-step recipe the agent (or the user) wants to reuse:
"how we cut a release", "how we triage a flaky test", "the steps to onboard a
new integration". Same shape and machinery as the skill registry
(:mod:`aiforge_core.runtime.skills`) — a directory per workflow holding a
``WORKFLOW.md`` with YAML frontmatter (``name`` / ``description`` /
``triggers``) and a markdown body — but kept in its own folder so skills
(small reusable how-tos) and workflows (longer end-to-end procedures) stay
separate and individually browsable.

Roots (all merged; repo-local overrides global by ``name``):
    $AIFORGE_WORKFLOWS_DIR or ~/.aiforge/workflows/<name>/WORKFLOW.md   (global)
    <repo>/.aiforge/workflows/<name>/WORKFLOW.md
    <repo>/.claude/workflows/<name>/WORKFLOW.md

New workflows are added two ways: the agent calls :func:`write_workflow` after
learning a repeatable procedure, or the user drops a ``WORKFLOW.md`` into the
folder by hand (picked up on next load).
"""
from __future__ import annotations

import os
from pathlib import Path

from aiforge_core.runtime import skills as _sk
from aiforge_core.runtime.skills import Skill  # reuse the same dataclass

_REPO_SUBDIRS = (".aiforge/workflows", ".claude/workflows", ".openhands/workflows")
_FILENAME = "WORKFLOW.md"


def _global_dir() -> Path:
    raw = os.environ.get("AIFORGE_WORKFLOWS_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".aiforge" / "workflows"


def _scan_dir(root: Path) -> list[Skill]:
    """Read ``<root>/<name>/WORKFLOW.md`` (dir form) AND ``<root>/*.md`` (flat)."""
    out: list[Skill] = []
    if not root.exists():
        return out
    try:
        for child in sorted(root.iterdir()):
            md: Path | None = None
            if child.is_dir():
                cand = child / _FILENAME
                if cand.is_file():
                    md = cand
            elif child.suffix == ".md":
                md = child
            if md is None:
                continue
            try:
                wf = _sk._parse_skill_md(
                    md.read_text(encoding="utf-8", errors="ignore"),
                    default_name=child.stem)
            except Exception:  # noqa: BLE001
                continue
            if wf is not None:
                out.append(Skill(**{**wf.__dict__, "source": str(md)}))
    except Exception:  # noqa: BLE001
        return out
    return out


def load(cwd: str | None = None) -> list[Skill]:
    """Global + repo-local workflows, de-duped by name (repo-local wins)."""
    by_name: dict[str, Skill] = {}
    for wf in _scan_dir(_global_dir()):
        by_name[wf.name] = wf
    root = _sk._repo_root(cwd)
    if root:
        for sub in _REPO_SUBDIRS:
            for wf in _scan_dir(Path(root) / sub):
                by_name[wf.name] = wf
    return list(by_name.values())


def search(query: str, cwd: str | None = None, k: int = 5) -> list[dict]:
    """Relevance-rank workflows for ``query`` (same scorer as skills)."""
    return _sk.search(query, cwd, k=k, skills=load(cwd))


def write_workflow(name: str, description: str, body: str,
                   triggers: list[str] | None = None, *,
                   cwd: str | None = None, scope: str = "global") -> dict:
    """Author/overwrite a reusable ``WORKFLOW.md``.

    ``scope`` = ``global`` (~/.aiforge/workflows) or ``repo``
    (<repo>/.aiforge/workflows). Returns ``{ok, name, path}`` or
    ``{ok: False, error}``. Also mirrored into the knowledge memory
    (``kind=workflow``) so it surfaces in cross-source recall."""
    name = (name or "").strip()
    body = (body or "").strip()
    if not name or not body:
        return {"ok": False, "error": "name and body are required"}
    if scope == "repo":
        root = _sk._repo_root(cwd)
        base = Path(root) / ".aiforge" / "workflows" if root else _global_dir()
    else:
        base = _global_dir()
    wf_dir = base / _sk._slug(name)
    trig = [t.strip().lower() for t in (triggers or []) if str(t).strip()]
    front = "---\n" + f"name: {name}\n"
    if description:
        front += f"description: {description.strip()}\n"
    if trig:
        front += "triggers: [" + ", ".join(trig) + "]\n"
    front += "---\n"
    try:
        wf_dir.mkdir(parents=True, exist_ok=True)
        path = wf_dir / _FILENAME
        path.write_text(front + "\n" + body + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    mem = False
    try:
        from aiforge_core.runtime.tools.memory_write import memory_write as _mw
        res = _mw(
            text=f"WORKFLOW: {name} — {description}".strip(" —")
                 + (f"\n{body[:600]}" if body else ""),
            kind="workflow",
            tags=["workflow", scope] + trig[:5],
            decision=False,
            repo=_sk._repo_name(cwd),
        )
        mem = bool(isinstance(res, dict) and res.get("ok", True))
    except Exception:  # noqa: BLE001
        mem = False
    return {"ok": True, "name": name, "path": str(path), "memory": mem}


def ensure_dirs() -> dict:
    """Create the global skills + workflows folders so they exist for the user
    to drop files into (and the registries to scan). Idempotent; best-effort."""
    out = {}
    for label, d in (("skills", _sk._global_dir()), ("workflows", _global_dir())):
        try:
            d.mkdir(parents=True, exist_ok=True)
            out[label] = str(d)
        except Exception as exc:  # noqa: BLE001
            out[label] = f"error: {exc}"
    return out


__all__ = ["load", "search", "write_workflow", "ensure_dirs"]
