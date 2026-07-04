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
import re
from pathlib import Path

from aiforge_core.runtime import skills as _sk
from aiforge_core.runtime.skills import Selection, Skill  # reuse the same types

_REPO_SUBDIRS = (".aiforge/workflows", ".claude/workflows", ".openhands/workflows")
_FILENAME = "WORKFLOW.md"


def _global_dir() -> Path:
    raw = os.environ.get("AIFORGE_WORKFLOWS_DIR")
    if raw:
        return Path(raw).expanduser()
    # Same config dir as the rest of the app (AIFORGE_CONFIG_DIR) — a raw
    # Path.home() diverges from the operator's configured/mounted dir on
    # docker/hybrid, so workflows built via chat landed outside it.
    cfg = os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge"))
    return Path(cfg) / "workflows"


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


def _builtin_dir() -> Path:
    """Shipped default workflows (lowest priority — custom always wins)."""
    return Path(__file__).resolve().parent / "builtin_playbooks" / "workflows"


def load(cwd: str | None = None) -> list[Skill]:
    """Workflows, de-duped by name. Priority: BUILT-IN defaults → global user →
    repo-local (later wins). A CUSTOM workflow overrides + outranks a default."""
    from dataclasses import replace as _replace
    by_name: dict[str, Skill] = {}
    for wf in _scan_dir(_builtin_dir()):
        by_name[wf.name] = _replace(wf, source="builtin", priority=wf.priority - 100)
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


def select(query: str, cwd: str | None = None, k: int = 3) -> list[Skill]:
    """The workflows :func:`auto_context` would inject for ``query`` — always-on
    + top-``k`` relevant, priority-ordered. Factored out so callers can both
    render the block AND report which workflows fired (workflow-transparency)."""
    pool = load(cwd)
    if not pool:
        return []
    chosen: dict[str, Skill] = {w.name: w for w in pool if w.always}
    for hit in search(query, cwd, k=k):
        w = next((x for x in pool if x.name == hit["name"]), None)
        if w is not None:
            chosen[w.name] = w
    return sorted(chosen.values(), key=lambda s: -s.priority)


def select_or_ask(query: str, cwd: str | None = None, k: int = 3) -> Selection:
    """Like :func:`select` but returns ambiguous near-ties separately
    instead of silently auto-picking (same scorer as skills.select_or_ask)."""
    return _sk.select_or_ask(query, cwd, k=k, pool=load(cwd))


def selected_names(query: str, cwd: str | None = None, k: int = 3) -> list[dict]:
    """``[{name, why}]`` for the workflows :func:`auto_context` injects — ``why``
    is ``always`` or ``match``. Drives the Workflow UI's "workflows used" badge."""
    always = {w.name for w in load(cwd) if w.always}
    return [{"name": w.name,
             "why": "always" if w.name in always else "match"}
            for w in select(query, cwd, k)]


def auto_context(query: str, cwd: str | None = None, k: int = 3) -> str:
    """Injection block: the top-``k`` workflows most relevant to ``query`` (plus
    any always-on ones), so the chat agent is reminded of reusable end-to-end
    procedures the same way it gets skills. Bodies are capped — the agent calls
    ``workflow_search`` for the full text. Empty when none apply."""
    chosen = select(query, cwd, k)
    if not chosen:
        return ""
    parts = []
    for w in chosen:
        head = f"### {w.name}" + (f" — {w.description}" if w.description else "")
        parts.append(f"{head}\n{w.body[:1200]}")
    return ("RELEVANT WORKFLOWS (reusable end-to-end procedures — follow when "
            "they fit; call workflow_search for the full text):\n"
            + "\n\n".join(parts))


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
    """Create the global skills/workflows/rules folders. We NO LONGER copy the
    bundled defaults into them — ``load()`` reads the builtin playbooks directly
    (as low-priority defaults), so the global dir is for USER-created playbooks
    only. Also MIGRATES away the old seeding: earlier versions copied every
    builtin into the global dir, which now double-shadows the (refined) builtin
    set. We remove those seeded copies (identified by ``source: builtin`` in the
    frontmatter) so the current default set is authoritative; user-authored files
    (any other source) are untouched. Runs once per migration version."""
    out: dict = {}
    from . import repo_rules as _rr
    for label, dest in (("skills", _sk._global_dir()),
                        ("workflows", _global_dir()),
                        ("rules", _rr._global_rules_dir())):
        try:
            dest.mkdir(parents=True, exist_ok=True)
            migrated = dest / ".builtins_migrated_v2"
            removed = 0
            if not migrated.exists():
                for f in list(dest.glob("*.md")) + list(dest.glob("*.mdc")):
                    try:
                        head = f.read_text(encoding="utf-8")[:400]
                    except Exception:  # noqa: BLE001
                        continue
                    # a seeded default carries `source: builtin` in its
                    # frontmatter; a user's own playbook does not.
                    if re.search(r"^\s*source:\s*builtin\s*$", head, re.M):
                        try:
                            f.unlink()
                            removed += 1
                        except Exception:  # noqa: BLE001
                            pass
                # drop the old seed marker so state is clean
                old = dest / ".builtins_seeded"
                if old.exists():
                    try:
                        old.unlink()
                    except Exception:  # noqa: BLE001
                        pass
                migrated.write_text("migrated\n", encoding="utf-8")
            out[label] = {"dir": str(dest), "removed_seeded": removed}
        except Exception as exc:  # noqa: BLE001
            out[label] = f"error: {exc}"
    return out


__all__ = ["load", "search", "select", "select_or_ask", "selected_names",
           "write_workflow", "ensure_dirs", "auto_context"]
