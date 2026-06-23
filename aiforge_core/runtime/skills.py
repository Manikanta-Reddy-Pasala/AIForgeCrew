"""Skill registry — ``SKILL.md`` standard + searchable, self-authored.

Closes two gaps vs Hermes Agent / OpenClaw:

  1. **Standard + registry.** Adopts the agentskills.io / ``SKILL.md``
     convention: a skill is a directory containing ``SKILL.md`` with YAML
     frontmatter (``name``, ``description``, optional ``triggers`` /
     ``when_to_use`` / ``always``) and a markdown body of instructions.
     Loaded from a global dir + per-repo dirs, then **searched by
     relevance** (description + trigger overlap), not just trigger
     substring — so the agent auto-discovers the right skill.

  2. **Self-improving.** :func:`write_skill` lets an agent author a
     reusable skill the moment it solves something hard (the Hermes
     model). Authored skills land in the registry and resurface on the
     next relevant request — the learning loop.

Back-compat: the loader also folds in the legacy single-file
``microagents`` so nothing already shipped is lost.

Roots (all merged; repo-local overrides global by ``name``):
    $AIFORGE_SKILLS_DIR or ~/.aiforge/skills/<name>/SKILL.md   (global)
    <repo>/.aiforge/skills/<name>/SKILL.md
    <repo>/.claude/skills/<name>/SKILL.md
    <repo>/.openhands/skills/<name>/SKILL.md
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None  # type: ignore

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_REPO_SUBDIRS = (".aiforge/skills", ".claude/skills", ".openhands/skills")
_MAX_BODY = 1800
_WORD_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str
    source: str = ""
    always: bool = False
    priority: int = 0


def _global_dir() -> Path:
    raw = os.environ.get("AIFORGE_SKILLS_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".aiforge" / "skills"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "skill").lower()).strip("-") or "skill"


def _parse_skill_md(text: str, default_name: str) -> Skill | None:
    m = _FRONTMATTER_RE.match(text or "")
    meta: dict = {}
    body = text or ""
    if m:
        body = m.group(2).strip()
        if yaml is not None:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except Exception:  # noqa: BLE001
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
    name = str(meta.get("name") or default_name).strip()
    desc = str(meta.get("description") or "").strip()
    triggers_raw = meta.get("triggers") or meta.get("when_to_use") or []
    if isinstance(triggers_raw, str):
        triggers_raw = [t.strip() for t in triggers_raw.split(",")]
    triggers = tuple(str(t).lower() for t in triggers_raw
                     if isinstance(t, str) and t.strip())
    always = str(meta.get("always", "")).lower() in ("true", "yes", "1") \
        or str(meta.get("type", "")).lower() == "repo"
    try:
        priority = int(meta.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0
    if not name or not body:
        return None
    return Skill(name=name, description=desc, triggers=triggers, body=body,
                 source="", always=always, priority=priority)


def _scan_dir(root: Path) -> list[Skill]:
    """Read ``<root>/<name>/SKILL.md`` (dir form) AND ``<root>/*.md`` (flat)."""
    out: list[Skill] = []
    if not root.exists():
        return out
    try:
        for child in sorted(root.iterdir()):
            md: Path | None = None
            if child.is_dir():
                cand = child / "SKILL.md"
                if cand.is_file():
                    md = cand
            elif child.suffix == ".md":
                md = child
            if md is None:
                continue
            try:
                sk = _parse_skill_md(md.read_text(encoding="utf-8", errors="ignore"),
                                     default_name=child.stem)
            except Exception:  # noqa: BLE001
                continue
            if sk is not None:
                out.append(Skill(**{**sk.__dict__, "source": str(md)}))
    except Exception:  # noqa: BLE001
        return out
    return out


def _repo_root(cwd: str | None) -> str | None:
    return (os.environ.get("AIFORGE_WORKSPACE_DIR") or cwd
            or os.environ.get("AIFORGE_REPO_ROOT") or None)


def _repo_name(cwd: str | None) -> str:
    root = _repo_root(cwd) or cwd or "."
    return os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "skills"


def load(cwd: str | None = None) -> list[Skill]:
    """Global + repo-local skills + legacy microagents, de-duped by name
    (repo-local / later wins). Best-effort."""
    by_name: dict[str, Skill] = {}
    for sk in _scan_dir(_global_dir()):
        by_name[sk.name] = sk
    root = _repo_root(cwd)
    if root:
        for sub in _REPO_SUBDIRS:
            for sk in _scan_dir(Path(root) / sub):
                by_name[sk.name] = sk
    # Fold legacy single-file microagents in (don't lose anything shipped).
    try:
        from aiforge_core.runtime import microagents as _ma
        for m in _ma.load_all(cwd):
            if m.name not in by_name:
                by_name[m.name] = Skill(
                    name=m.name, description="", triggers=tuple(m.triggers),
                    body=m.body, source=m.source,
                    always=(m.type == "repo"), priority=m.priority)
    except Exception:  # noqa: BLE001
        pass
    return list(by_name.values())


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def search(query: str, cwd: str | None = None, k: int = 5,
           skills: list[Skill] | None = None) -> list[dict]:
    """Relevance-rank skills for ``query``. Score = exact-trigger boost +
    token overlap with name/description/triggers. Returns
    ``[{name, description, score, source}]`` top-k (score > 0)."""
    pool = skills if skills is not None else load(cwd)
    q = (query or "").lower()
    qtok = _tokens(query)
    scored: list[tuple[float, Skill]] = []
    for sk in pool:
        score = 0.0
        for t in sk.triggers:
            if t and t in q:
                score += 5.0                       # exact trigger hit
        overlap = qtok & _tokens(sk.name + " " + sk.description
                                 + " " + " ".join(sk.triggers))
        score += float(len(overlap))
        if sk.always:
            score += 0.5
        if score > 0:
            scored.append((score + sk.priority * 0.1, sk))
    scored.sort(key=lambda x: -x[0])
    return [{"name": s.name, "description": s.description,
             "score": round(sc, 2), "source": s.source}
            for sc, s in scored[:k]]


def render(skills: list[Skill]) -> str:
    if not skills:
        return ""
    parts = []
    for sk in skills:
        head = f"### {sk.name}" + (f" — {sk.description}" if sk.description else "")
        parts.append(f"{head}\n{sk.body[:_MAX_BODY]}")
    return ("RELEVANT SKILLS (reusable playbooks — apply when they fit):\n"
            + "\n\n".join(parts))


def auto_context(query: str, cwd: str | None = None, k: int = 4) -> str:
    """Injection block: all always-on skills + the top-``k`` relevant ones
    for ``query`` (by :func:`search`). Empty when none apply."""
    pool = load(cwd)
    # Cap always-on skills so a large registry can't blow the context budget
    # every turn (highest-priority always-on win).
    always_cap = int(os.environ.get("AIFORGE_SKILLS_ALWAYS_CAP", "8"))
    always_on = sorted((s for s in pool if s.always),
                       key=lambda s: -s.priority)[:always_cap]
    chosen: dict[str, Skill] = {s.name: s for s in always_on}
    for hit in search(query, cwd, k=k, skills=pool):
        sk = next((s for s in pool if s.name == hit["name"]), None)
        if sk is not None:
            chosen[sk.name] = sk
    ordered = sorted(chosen.values(), key=lambda s: -s.priority)
    return render(ordered)


def write_skill(name: str, description: str, body: str,
                triggers: list[str] | None = None, *,
                cwd: str | None = None, scope: str = "global") -> dict:
    """Author/overwrite a reusable ``SKILL.md`` (self-improvement loop).

    ``scope`` = ``global`` (~/.aiforge/skills) or ``repo`` (<repo>/.aiforge/
    skills). Returns ``{ok, name, path}`` or ``{ok: False, error}``."""
    name = (name or "").strip()
    body = (body or "").strip()
    if not name or not body:
        return {"ok": False, "error": "name and body are required"}
    if scope == "repo":
        root = _repo_root(cwd)
        base = Path(root) / ".aiforge" / "skills" if root else _global_dir()
    else:
        base = _global_dir()
    skill_dir = base / _slug(name)
    fm = {"name": name}
    if description:
        fm["description"] = description.strip()
    trig = [t.strip().lower() for t in (triggers or []) if str(t).strip()]
    front = "---\n"
    front += f"name: {fm['name']}\n"
    if description:
        front += f"description: {description.strip()}\n"
    if trig:
        front += "triggers: [" + ", ".join(trig) + "]\n"
    front += "---\n"
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        path.write_text(front + "\n" + body + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    # Also record the learning in the knowledge memory so unified_query /
    # the graph surface it alongside facts — the SKILL.md is the executable
    # playbook, the memory entry makes it retrievable cross-source.
    mem = False
    try:
        from aiforge_core.runtime.tools.memory_write import memory_write as _mw
        repo = _repo_name(cwd)
        res = _mw(
            text=f"SKILL: {name} — {description}".strip(" —")
                 + (f"\n{body[:600]}" if body else ""),
            kind="skill",
            tags=["skill", scope] + ([t.strip().lower() for t in (triggers or [])][:5]),
            decision=False,
            repo=repo,
        )
        mem = bool(isinstance(res, dict) and res.get("ok", True))
    except Exception:  # noqa: BLE001
        mem = False
    return {"ok": True, "name": name, "path": str(path), "memory": mem}


__all__ = ["Skill", "load", "search", "render", "auto_context", "write_skill"]
