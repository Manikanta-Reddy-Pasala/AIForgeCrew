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
import subprocess
from pathlib import Path

from aiforge_core.runtime import skills as _sk
from aiforge_core.runtime.skills import Selection, Skill  # reuse the same types

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


_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_script_syntax(path: Path) -> str | None:
    """Static syntax check for a helper script — returns an error string or
    None. Best-effort per language: bash -n for shell, py_compile for python,
    node --check for js when node exists. Unknown extensions pass (there is no
    checker to run)."""
    ext = path.suffix.lower()
    try:
        if ext in (".sh", ".bash"):
            r = subprocess.run(["bash", "-n", str(path)], capture_output=True,
                               text=True, timeout=15)
            if r.returncode != 0:
                return (r.stderr or r.stdout or "bash -n failed").strip()[:500]
        elif ext == ".py":
            import py_compile
            try:
                py_compile.compile(str(path), doraise=True)
            except py_compile.PyCompileError as exc:
                return str(exc)[:500]
        elif ext in (".js", ".mjs") and shutil.which("node"):
            r = subprocess.run(["node", "--check", str(path)],
                               capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return (r.stderr or r.stdout or "node --check failed").strip()[:500]
    except Exception:  # noqa: BLE001 — a missing/broken checker must not block authoring
        return None
    return None


def _normalize_scripts(scripts) -> tuple[list[tuple[str, str, str]], str | None]:
    """Validate the ``scripts`` argument into ``[(filename, content, test)]``.
    Accepts a list of {name, content, test?} dicts or a {name: content}
    mapping. ``test`` is the shell command the HARD gate runs to prove the
    script works ("" → run the script itself; "skip" → explicitly untestable,
    e.g. needs prod-only state). Rejects path traversal (any separator in the
    name) and empty content."""
    if not scripts:
        return [], None
    if isinstance(scripts, dict):
        scripts = [{"name": k, "content": v} for k, v in scripts.items()]
    if not isinstance(scripts, list):
        return [], "scripts must be a list of {name, content}"
    out: list[tuple[str, str, str]] = []
    for s in scripts:
        if not isinstance(s, dict):
            return [], "each script must be a {name, content} object"
        fname = str(s.get("name") or s.get("filename") or "").strip()
        content = s.get("content") or s.get("body") or ""
        test = str(s.get("test") or "").strip()
        if not fname or not _SCRIPT_NAME_RE.match(fname):
            return [], f"invalid script name {fname!r} (plain filename only, no paths)"
        if not isinstance(content, str) or not content.strip():
            return [], f"script {fname!r} has no content"
        out.append((fname, content, test))
    return out, None


_SCRIPT_RUNNER_BY_EXT = {".sh": "bash", ".bash": "bash", ".py": "python3",
                         ".js": "node", ".mjs": "node", ".rb": "ruby",
                         ".pl": "perl"}


def _test_scripts_hard(staged_dir: Path,
                       script_files: list[tuple[str, str, str]]) -> str | None:
    """HARD gate (job-builder parity): actually RUN each staged script — its
    declared ``test`` command, else the script itself with no args — inside
    the staging dir. Returns an error string (with output tail) on the first
    failure; a workflow with a failing script is never saved. ``test: skip``
    opts a genuinely-untestable script out (prod-only state) — the builder
    charter requires justifying that in the body."""
    timeout = 60
    try:
        timeout = max(5, int(os.environ.get(
            "AIFORGE_WORKFLOW_SCRIPT_TEST_TIMEOUT_S", "60")))
    except ValueError:
        timeout = 60
    for fname, _content, test in script_files:
        if test.lower() == "skip":
            continue
        if test:
            cmd = test
        else:
            runner = _SCRIPT_RUNNER_BY_EXT.get(Path(fname).suffix.lower())
            if runner is None:
                continue           # no way to execute (e.g. .sql) — syntax-only
            if runner in ("node", "ruby", "perl") and not shutil.which(runner):
                continue           # interpreter absent on this host
            cmd = f"{runner} {fname}"
        try:
            r = subprocess.run(cmd, shell=True, cwd=str(staged_dir),
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return (f"script {fname!r} test timed out after {timeout}s "
                    f"(cmd: {cmd}) — make it terminate, or give it a fast "
                    "--dry-run 'test' command")
        if r.returncode != 0:
            tail = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()[-800:]
            return (f"script {fname!r} FAILED its test run (cmd: {cmd}, "
                    f"exit {r.returncode}) — fix it and retry; a workflow "
                    f"with a failing script is never saved:\n{tail}")
    return None


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
    if scope == "repo":
        root = _sk._repo_root(cwd)
        base = Path(root) / ".aiforge" / "workflows" if root else _global_dir()
    else:
        base = _global_dir()
    wf_dir = base / _sk._slug(name)
    trig = [t.strip().lower() for t in (triggers or []) if str(t).strip()]
    import json as _json
    # OKF v0.1: `type:` required; `name` = OKF title; triggers/scope preserved.
    front = "---\n" + "type: workflow\n" + "name: " + _json.dumps(name) + "\n"
    if description:
        front += "description: " + _json.dumps(description.strip()) + "\n"
    if trig:
        front += "triggers: [" + ", ".join(_json.dumps(t) for t in trig) + "]\n"
    front += "scope: " + _json.dumps((scope or "global").lower()) + "\n"
    front += "---\n"
    script_paths: list[str] = []
    try:
        # Stage scripts in a scratch dir FIRST: syntax-check each, then the
        # HARD gate actually RUNS them (test command or the script itself,
        # job-builder parity) — any failure aborts the whole write, so a
        # broken workflow is never saved.
        if script_files:
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                for fname, content, _test in script_files:
                    sp = Path(td) / fname
                    sp.write_text(content, encoding="utf-8")
                    sp.chmod(0o755)
                    serr = _check_script_syntax(sp)
                    if serr:
                        return {"ok": False,
                                "error": f"script {fname!r} failed its syntax "
                                         f"check — fix and retry: {serr}"}
                terr = _test_scripts_hard(Path(td), script_files)
                if terr:
                    return {"ok": False, "error": terr}
        wf_dir.mkdir(parents=True, exist_ok=True)
        if script_files:
            sdir = wf_dir / "scripts"
            sdir.mkdir(parents=True, exist_ok=True)
            for fname, content, _test in script_files:
                dst = sdir / fname
                dst.write_text(content, encoding="utf-8")
                dst.chmod(0o755)
                script_paths.append(str(dst))
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
    out = {"ok": True, "name": name, "path": str(path), "memory": mem}
    if script_paths:
        out["scripts"] = script_paths
    return out


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


def _deletable_roots(cwd: str | None) -> list[Path]:
    roots = [_global_dir(), _builtin_dir()]
    root = _sk._repo_root(cwd)
    if root:
        roots += [Path(root) / sub for sub in _REPO_SUBDIRS]
    return roots


def delete_workflow(name: str, cwd: str | None = None) -> dict:
    """Delete the workflow(s) named ``name`` by unlinking the backing file
    (custom OR shipped default), bounded to the playbook dirs."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    roots = [r.resolve() for r in _deletable_roots(cwd)]
    removed: list[str] = []
    for wf in load(cwd):
        if wf.name != name:
            continue
        src = getattr(wf, "source", "")
        if not src or src == "builtin":
            continue
        p = Path(src)
        if not any(_sk._within(p, r) for r in roots):
            continue
        try:
            p.unlink()
            # dir form: remove the slug dir INCLUDING its scripts/ folder,
            # but never a root itself
            if p.parent.resolve() not in roots and p.parent.is_dir():
                leftovers = list(p.parent.iterdir())
                if not leftovers:
                    p.parent.rmdir()
                elif (p.name == _FILENAME
                      and all(x.name == "scripts" for x in leftovers)):
                    shutil.rmtree(p.parent, ignore_errors=True)
            removed.append(str(p))
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
    if not removed:
        return {"ok": False, "error": f"no deletable workflow named {name!r}"}
    return {"ok": True, "name": name, "removed": removed}


def clear_workflows(cwd: str | None = None) -> dict:
    names = {w.name for w in load(cwd)}
    removed = 0
    for n in names:
        r = delete_workflow(n, cwd)
        if r.get("ok"):
            removed += len(r.get("removed", []))
    return {"ok": True, "removed": removed}


__all__ = ["load", "search", "select", "select_or_ask", "selected_names",
           "write_workflow", "ensure_dirs", "auto_context", "scripts_for",
           "delete_workflow", "clear_workflows"]
