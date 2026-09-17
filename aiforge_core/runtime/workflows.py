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
import shutil
from pathlib import Path

from aiforge_core.config.paths import config_dir
from aiforge_core.runtime import skills as _sk
from aiforge_core.runtime.skills import Selection, Skill  # reuse the same types

from ._workflow_scripts import (  # noqa: F401  # re-exported
    _SCRIPT_NAME_RE,
    _SCRIPT_RUNNER_BY_EXT,
    _check_script_syntax,
    _mirror_to_memory,
    _normalize_scripts,
    _proc_error,
    _py_syntax_error,
    _run_script_test,
    _script_test_cmd,
    _test_scripts_hard,
    _vet_scripts,
    _workflow_base,
    _workflow_frontmatter,
    _write_scripts,
)

_REPO_SUBDIRS = (".aiforge/workflows", ".claude/workflows")
_FILENAME = "WORKFLOW.md"

# Per-workflow body budget in the injected block. A workflow with an ordered
# procedure plus a strict output/naming convention needs room — truncating
# silently drops the very steps the user needs honoured. Env-tunable.
try:
    _WF_MAX_BODY = max(400, int(os.environ.get("AIFORGE_WORKFLOW_MAX_BODY", "3000")))
except ValueError:
    _WF_MAX_BODY = 3000


def _global_dir() -> Path:
    raw = os.environ.get("AIFORGE_WORKFLOWS_DIR")
    if raw:
        return Path(raw).expanduser()
    # Same config dir as the rest of the app (AIFORGE_CONFIG_DIR) — a raw
    # Path.home() diverges from the operator's configured/mounted dir on
    # docker/hybrid, so workflows built via chat landed outside it.
    cfg = str(config_dir())
    return Path(cfg) / "workflows"


def _workflow_md(child: Path) -> Path | None:
    """The markdown for one entry — ``<name>/WORKFLOW.md`` (dir form) or a flat
    ``*.md``."""
    if child.is_dir():
        cand = child / _FILENAME
        return cand if cand.is_file() else None
    return child if child.suffix == ".md" else None


def _scan_dir(root: Path) -> list[Skill]:
    """Read ``<root>/<name>/WORKFLOW.md`` (dir form) AND ``<root>/*.md`` (flat)."""
    out: list[Skill] = []
    if not root.exists():
        return out
    try:
        children = sorted(root.iterdir())
    except Exception:  # noqa: BLE001
        return out
    for child in children:
        md = _workflow_md(child)
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
    return out


def _builtin_dir() -> Path:
    """Shipped default workflows (lowest priority — custom always wins)."""
    return Path(__file__).resolve().parent / "builtin_playbooks" / "workflows"


def load(cwd: str | None = None) -> list[Skill]:
    """Workflows, de-duped by name. Priority: BUILT-IN defaults → global user →
    repo-local (later wins). A CUSTOM workflow overrides + outranks a default."""
    from dataclasses import replace as _replace

    from aiforge_core.runtime import library_defaults
    off = library_defaults.disabled("workflow")
    by_name: dict[str, Skill] = {}
    for wf in _scan_dir(_builtin_dir()):
        if wf.name in off:      # disabled on this box; the shipped file stays
            continue
        by_name[wf.name] = _replace(wf, source="builtin", priority=wf.priority - 100)
    for wf in _scan_dir(_global_dir()):
        by_name[wf.name] = wf
    root = _sk._repo_root(cwd)
    if root:
        for sub in _REPO_SUBDIRS:
            for wf in _scan_dir(Path(root) / sub):
                by_name[wf.name] = wf
    return list(by_name.values())


def _scripts_dir(md_source: str) -> Path | None:
    """Scripts folder for a workflow: ``<dir>/scripts`` next to its
    ``WORKFLOW.md`` (dir form only — flat ``*.md`` workflows have no folder to
    hold scripts, and builtins carry the ``builtin`` sentinel, not a path)."""
    if not md_source or md_source == "builtin":
        return None
    p = Path(md_source)
    if p.name != _FILENAME:
        return None
    d = p.parent / "scripts"
    return d if d.is_dir() else None


def scripts_for(md_source: str) -> list[str]:
    """Absolute paths of a workflow's helper scripts (empty when none)."""
    d = _scripts_dir(md_source)
    if d is None:
        return []
    try:
        return sorted(str(f) for f in d.iterdir() if f.is_file()
                      and not f.name.startswith("."))
    except Exception:  # noqa: BLE001
        return []


def search(query: str, cwd: str | None = None, k: int = 5) -> list[dict]:
    """Relevance-rank workflows for ``query`` (same scorer as skills). Hits
    additionally carry ``scripts`` (the workflow's helper-script paths) so the
    agent can run them instead of re-deriving the commands."""
    hits = _sk.search(query, cwd, k=k, skills=load(cwd))
    for h in hits:
        scripts = scripts_for(h.get("source") or "")
        if scripts:
            h["scripts"] = scripts
    return hits


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
        block = f"{head}\n{w.body[:_WF_MAX_BODY]}"
        scripts = scripts_for(getattr(w, "source", "") or "")
        if scripts:
            block += ("\n(helper scripts — RUN these with run_command instead "
                      "of re-deriving the commands: " + ", ".join(scripts) + ")")
        parts.append(block)
    return ("APPLICABLE WORKFLOWS — when a procedure below matches the request, "
            "follow its steps IN ORDER and honour any output format or naming "
            "convention it specifies EXACTLY (every label and delimiter). When "
            "a workflow prescribes the exact output, produce it DIRECTLY — do "
            "not ask a clarifying question or add preamble first. Call "
            "workflow_search for the full text if a body looks truncated:\n"
            + "\n\n".join(parts))


def write_workflow(name: str, description: str, body: str,
                   triggers: list[str] | None = None, *,
                   cwd: str | None = None, scope: str = "global",
                   scripts: list | dict | None = None) -> dict:
    """Author/overwrite a reusable ``WORKFLOW.md``.

    ``scope`` = ``global`` (~/.aiforge/workflows) or ``repo``
    (<repo>/.aiforge/workflows). ``scripts`` (optional) = helper scripts to
    keep NEXT TO the workflow in ``<name>/scripts/`` — each is syntax-checked
    (bash -n / py_compile / node --check) and made executable; a failing
    script aborts the whole write so a broken workflow is never saved.
    Returns ``{ok, name, path, scripts}`` or ``{ok: False, error}``. Also
    mirrored into the knowledge memory (``kind=workflow``) so it surfaces in
    cross-source recall."""
    name = (name or "").strip()
    body = (body or "").strip()
    if not name or not body:
        return {"ok": False, "error": "name and body are required"}
    script_files, err = _normalize_scripts(scripts)
    if err:
        return {"ok": False, "error": err}
    vet = _vet_scripts(script_files)
    if vet:
        return {"ok": False, "error": vet}
    trig = [t.strip().lower() for t in (triggers or []) if str(t).strip()]
    wf_dir = _workflow_base(scope, cwd) / _sk._slug(name)
    try:
        wf_dir.mkdir(parents=True, exist_ok=True)
        script_paths = _write_scripts(wf_dir, script_files)
        path = wf_dir / _FILENAME
        path.write_text(
            _workflow_frontmatter(name, description, trig, scope)
            + "\n" + body + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    out = {"ok": True, "name": name, "path": str(path),
           "memory": _mirror_to_memory(name, description, body, scope, trig, cwd)}
    if script_paths:
        out["scripts"] = script_paths
    return out


def _is_seeded_builtin(f: Path) -> bool:
    """A seeded default carries ``source: builtin`` in its frontmatter; a
    user's own playbook does not."""
    try:
        head = f.read_text(encoding="utf-8")[:400]
    except Exception:  # noqa: BLE001
        return False
    return bool(re.search(r"^\s*source:\s*builtin\s*$", head, re.M))


def _drop_seeded_builtins(dest: Path) -> int:
    """Remove the copies an earlier version seeded into the global dir, which
    now double-shadow the (refined) builtin set. Returns how many went."""
    removed = 0
    for f in list(dest.glob("*.md")) + list(dest.glob("*.mdc")):
        if not _is_seeded_builtin(f):
            continue
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
    return removed


def _ensure_playbook_dir(dest: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    migrated = dest / ".builtins_migrated_v2"
    removed = 0
    if not migrated.exists():
        removed = _drop_seeded_builtins(dest)
        migrated.write_text("migrated\n", encoding="utf-8")
    return {"dir": str(dest), "removed_seeded": removed}


def ensure_dirs() -> dict:
    """Create the global skills/workflows/rules folders. We NO LONGER copy the
    bundled defaults into them — ``load()`` reads the builtin playbooks directly
    (as low-priority defaults), so the global dir is for USER-created playbooks
    only. Also MIGRATES away the old seeding: earlier versions copied every
    builtin into the global dir, which now double-shadows the (refined) builtin
    set. We remove those seeded copies (identified by ``source: builtin`` in the
    frontmatter) so the current default set is authoritative; user-authored files
    (any other source) are untouched. Runs once per migration version."""
    from . import repo_rules as _rr
    out: dict = {}
    for label, dest in (("skills", _sk._global_dir()),
                        ("workflows", _global_dir()),
                        ("rules", _rr._global_rules_dir())):
        try:
            out[label] = _ensure_playbook_dir(dest)
        except Exception as exc:  # noqa: BLE001
            out[label] = f"error: {exc}"
    return out


def _deletable_roots(cwd: str | None) -> list[Path]:
    roots = [_global_dir(), _builtin_dir()]
    root = _sk._repo_root(cwd)
    if root:
        roots += [Path(root) / sub for sub in _REPO_SUBDIRS]
    return roots


def _prune_workflow_dir(p: Path, roots: list) -> None:
    """Dir form: remove the slug dir INCLUDING its scripts/ folder — but never
    a root itself."""
    parent = p.parent
    if parent.resolve() in roots or not parent.is_dir():
        return
    leftovers = list(parent.iterdir())
    if not leftovers:
        parent.rmdir()
    elif p.name == _FILENAME and all(x.name == "scripts" for x in leftovers):
        shutil.rmtree(parent, ignore_errors=True)


def _deletable_path(wf, roots: list) -> Path | None:
    """The backing file, when it is a real file inside a playbook dir. A
    builtin (the ``builtin`` sentinel, not a path) is never deletable."""
    src = getattr(wf, "source", "")
    if not src or src == "builtin":
        return None
    p = Path(src)
    return p if any(_sk._within(p, r) for r in roots) else None


def _unlink_workflow(wf, roots, removed: list) -> "str | None":
    """Delete one custom workflow's file, appending it to ``removed``. Returns
    an error message when the unlink failed, else None (a file already gone, or
    one outside the deletable roots, is not an error)."""
    p = _deletable_path(wf, roots)
    if p is None:
        return None
    try:
        p.unlink()
        _prune_workflow_dir(p, roots)
        removed.append(str(p))
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


def delete_workflow(name: str, cwd: str | None = None) -> dict:
    """Remove the workflow named ``name``.

    A custom workflow is unlinked; a SHIPPED DEFAULT is disabled on this box
    instead — see :func:`skills.delete_skill` for why the package's own files
    are never touched."""
    from aiforge_core.runtime import library_defaults
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    roots = [r.resolve() for r in _deletable_roots(cwd)]
    removed: list[str] = []
    disabled = False
    for wf in load(cwd):
        if wf.name != name:
            continue
        if library_defaults.is_builtin(getattr(wf, "source", "")):
            disabled = library_defaults.disable("workflow", name) or disabled
            continue
        err = _unlink_workflow(wf, roots, removed)
        if err:
            return {"ok": False, "error": err}
    if not removed and not disabled:
        return {"ok": False, "error": f"no deletable workflow named {name!r}"}
    return {"ok": True, "name": name, "removed": removed, "disabled": disabled}


def clear_workflows(cwd: str | None = None) -> dict:
    """Clear the CUSTOM workflows; shipped defaults stay (see clear_skills)."""
    from aiforge_core.runtime import library_defaults
    names = {w.name for w in load(cwd)
             if not library_defaults.is_builtin(getattr(w, "source", ""))}
    removed = 0
    for n in names:
        r = delete_workflow(n, cwd)
        if r.get("ok"):
            removed += len(r.get("removed", []))
    return {"ok": True, "removed": removed}


__all__ = ["load", "search", "select", "select_or_ask", "selected_names",
           "write_workflow", "ensure_dirs", "auto_context", "scripts_for",
           "delete_workflow", "clear_workflows"]
