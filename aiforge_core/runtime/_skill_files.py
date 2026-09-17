"""Writing, recording and deleting skill files."""
from __future__ import annotations

from pathlib import Path


def _pkg():
    """``skills``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``skills``; patch any other
    name on this module."""
    import aiforge_core.runtime.skills as package
    return package


def _skill_frontmatter(name: str, description: str, trig: list[str],
                       scope: str) -> str:
    """Render the OKF v0.1 SKILL.md front-matter block. ``type:`` is the one
    required field; ``name`` doubles as the OKF title; triggers/scope are
    preserved custom keys."""
    import json as _json
    front = "---\ntype: skill\n"
    front += "name: " + _json.dumps(name) + "\n"
    if description:
        front += "description: " + _json.dumps(description.strip()) + "\n"
    if trig:
        front += "triggers: [" + ", ".join(_json.dumps(t) for t in trig) + "]\n"
    front += "scope: " + _json.dumps((scope or "global").lower()) + "\n"
    front += "---\n"
    return front


def _record_skill_memory(name: str, description: str, body: str,
                         triggers: list[str] | None, scope: str,
                         cwd: str | None) -> bool:
    """Mirror the skill into knowledge memory so unified_query / the graph
    surface it alongside facts. Best-effort — the SKILL.md is the executable
    playbook; this entry just makes it retrievable cross-source."""
    try:
        from aiforge_core.runtime.tools.memory_write import memory_write as _mw
        res = _mw(
            text=f"SKILL: {name} — {description}".strip(" —")
                 + (f"\n{body[:600]}" if body else ""),
            kind="skill",
            tags=["skill", scope]
                 + ([t.strip().lower() for t in (triggers or [])][:5]),
            decision=False, repo=_pkg()._repo_name(cwd))
        return bool(isinstance(res, dict) and res.get("ok", True))
    except Exception:  # noqa: BLE001
        return False


def write_skill(name: str, description: str, body: str,
                triggers: list[str] | None = None, *,
                cwd: str | None = None, scope: str = "global") -> dict:
    """Author/overwrite a reusable ``SKILL.md`` (self-improvement loop).

    ``scope`` = ``global`` (~/.aiforge/skills) or ``repo`` (<repo>/.aiforge/
    skills). Returns ``{ok, name, path}`` or ``{ok: False, error}``."""
    pkg = _pkg()
    name = (name or "").strip()
    body = (body or "").strip()
    if not name or not body:
        return {"ok": False, "error": "name and body are required"}
    if scope == "repo":
        root = pkg._repo_root(cwd)
        base = Path(root) / ".aiforge" / "skills" if root else pkg._global_dir()
    else:
        base = pkg._global_dir()
    skill_dir = base / pkg._slug(name)
    trig = [t.strip().lower() for t in (triggers or []) if str(t).strip()]
    front = _skill_frontmatter(name, description, trig, scope)
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / pkg._SKILL_MD
        path.write_text(front + "\n" + body + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    mem = _record_skill_memory(name, description, body, triggers, scope, cwd)
    return {"ok": True, "name": name, "path": str(path), "memory": mem}


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def _deletable_roots(cwd: str | None) -> list[Path]:
    """Dirs a skill file may be unlinked from: global, the shipped builtin dir,
    and repo-local playbook dirs. Bounds delete so it can never remove an
    arbitrary file outside the playbook tree."""
    pkg = _pkg()
    roots = [pkg._global_dir(), pkg._builtin_dir()]
    root = pkg._repo_root(cwd)
    if root:
        roots += [Path(root) / sub for sub in pkg._REPO_SUBDIRS]
    return roots


def _unlink_skill_file(src: str, roots) -> "str | None":
    """Unlink one skill's backing file if it lives under a deletable root; also
    drop a now-empty ``<name>/`` dir left by the SKILL.md form. Returns the path
    removed, or None (synthetic/out-of-bounds/already gone). Raises OSError on a
    real unlink failure the caller surfaces."""
    if not src or src == "builtin":
        return None                # no on-disk path (already gone / synthetic)
    p = Path(src)
    if not any(_within(p, r) for r in roots):
        return None
    try:
        p.unlink()
    except FileNotFoundError:
        return None
    if p.name == _pkg()._SKILL_MD and p.parent.is_dir() and not any(p.parent.iterdir()):
        p.parent.rmdir()
    return str(p)


def delete_skill(name: str, cwd: str | None = None) -> dict:
    """Remove the skill named ``name``.

    A custom skill is unlinked. A SHIPPED DEFAULT is disabled on this box
    instead (:mod:`runtime.library_defaults`) — the file belongs to the package,
    the next upgrade restores it, and a read-only install cannot unlink it at
    all. Returns ``{ok, removed:[paths], disabled}`` or ``{ok: False, error}``."""
    from aiforge_core.runtime import library_defaults
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    roots = _deletable_roots(cwd)
    removed: list[str] = []
    disabled = False
    for sk in _pkg().load(cwd):
        if sk.name != name:
            continue
        src = getattr(sk, "source", "")
        if library_defaults.is_builtin(src):
            disabled = library_defaults.disable("skill", name) or disabled
            continue
        try:
            got = _unlink_skill_file(src, roots)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if got:
            removed.append(got)
    if not removed and not disabled:
        return {"ok": False, "error": f"no deletable skill named {name!r}"}
    return {"ok": True, "name": name, "removed": removed, "disabled": disabled}


def clear_skills(cwd: str | None = None) -> dict:
    """Delete every CUSTOM skill. Shipped defaults are left in place — one
    "clear" must not silently switch off the company's playbooks (disable them
    one by one if that is really what you want). Returns the count removed."""
    from aiforge_core.runtime import library_defaults
    names = {s.name for s in _pkg().load(cwd)
             if not library_defaults.is_builtin(getattr(s, "source", ""))}
    removed = 0
    for n in names:
        r = delete_skill(n, cwd)
        if r.get("ok"):
            removed += len(r.get("removed", []))
    return {"ok": True, "removed": removed}
