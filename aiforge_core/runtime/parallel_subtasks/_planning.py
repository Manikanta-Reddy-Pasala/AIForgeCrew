"""Architect / Decompose planning + workspace baseline helpers.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical).
The prompt enhancer lives in ``_planning_enhance`` and plan-shape checks in
``_planning_shape``."""
from __future__ import annotations

import os

from pydantic import BaseModel

from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS, ensure_artifact_gitignore

from ._planning_enhance import (  # noqa: F401  # re-exported
    _ACTION_VERBS,
    _CODE_EXTS,
    _CONVERSATIONAL,
    _ENHANCE_SYS,
    _MULTIPART_RE,
    _VERB_RE,
    _enhance,
    _enhancer_disabled,
    _enhancer_min_chars,
    _enhancer_skip_concrete_enabled,
    _history_block,
    _is_concrete_prompt,
    _is_trivial_prompt,
    _memory_block,
    _names_a_code_file,
    _orchestrator_timeout_s,
    _readme_block,
    _spec_degenerate,
    _whole_conversational,
    enhance,
)
from ._planning_shape import (  # noqa: F401  # re-exported
    _COMPILED_CODE_EXTS,
    _PLAN_CODE_EXTS,
    _SYMBOL_DECL_RE,
    _api_symbols,
    _coalesce_code_modules,
    _coupled_pair,
    _fold_into,
    _importable_py_path,
    _max_code_modules,
    _max_compiled_modules,
    _merge_modules,
    _module_cap_for,
    _plan_path,
    _plan_shape_issues,
    _sanitized_files,
    _user_of,
    _validate_plan,
)

# ─────────────── Parallel chat mode (decompose → fan-out → merge) ──────────

_DECOMPOSE_SYS = (
    "You are a planner. Split the task into 3-8 subtasks that run IN PARALLEL. "
    "CRITICAL: each subtask must own a DISTINCT file (or files) — NO two subtasks "
    "may edit the same file, or they will merge-conflict. Put the target file in "
    "the goal, e.g. 'db.py: SQLite store + models'. One file per concern "
    "(db.py, models.py, slug.py, routes.py, main.py, test_app.py, README.md). "
    "Output ONLY: {\"subtickets\": [{\"slug\": \"kebab-id\", \"goal\": "
    "\"<file>: <what>\"}, ...]}. No prose."
)


_ARCHITECT_SYS = (
    "You are the architect. Given a build spec, design the FILE STRUCTURE **and "
    "the exact public API of each file**, because each file is implemented by a "
    "SEPARATE worker in isolation — they can only agree if you fix the shared "
    "contract now. Files must be DISJOINT (single responsibility). Honor any "
    "provided skills, workflows, and repo rules.\n\n"
    "DERIVE EVERYTHING FROM THE SPEC. Every file path, module name, class, and "
    "function MUST come from the modules and functions the SPEC names — do NOT "
    "invent unrelated files, and do NOT copy names from the example below (it "
    "shows JSON FORMAT ONLY, not content). If the spec says a `dates` module with "
    "`days_between`, design `dates.py` exposing `days_between` — never a `game.py` "
    "or `storage.py` the spec never mentioned. When the spec adds ONE module to an "
    "existing package, design ONLY that module's file(s) + its test(s).\n\n"
    "DEPENDENCY CLUSTERING — critical for correctness. Tightly-coupled logic that "
    "shares mutable state or arbitrary conventions (the same constants, the same "
    "in-memory model, the same matrix/state machine) MUST live in ONE file owned "
    "by ONE worker — do NOT atomise a coupled subsystem across files, or separate "
    "workers invent conflicting conventions that never reconcile. Rule: if two "
    "units must edit or assume the same state/constants, COLLAPSE them into a "
    "single file. Give a separate file only to a genuinely DECOUPLED concern (a "
    "persistence layer, a CLI/entrypoint, rendering behind a clean interface). "
    "Prefer a few cohesive files over many fragile ones.\n\n"
    "For every file give its exact PUBLIC API: the class names, function "
    "signatures, and module-level constants that OTHER files import or call — "
    "spelled EXACTLY as everyone must use them (one canonical name per thing). "
    "Use real signatures (names, params, return types where knowable).\n\n"
    "ALWAYS include, in the SAME file list: (a) a TEST file for EVERY code "
    "module (unit tests that exercise its public API), (b) at least one "
    "INTEGRATION test that drives the whole thing end-to-end, and (c) the "
    "project's build/manifest file (pyproject.toml / package.json / go.mod / "
    "pom.xml / Cargo.toml as fits the language). The tests are what lets the "
    "build be verified — never omit them.\n\n"
    "Output ONLY JSON, no prose. The example shows FORMAT ONLY — replace every "
    "name with names DERIVED FROM THE SPEC:\n"
    "{\"files\": [{\"path\": \"<module_from_spec>.py\", \"purpose\": \"<what the "
    "spec says this module does>\", \"api\": [\"def <function_from_spec>(...) -> "
    "<type>\"]}, {\"path\": \"tests/test_<module_from_spec>.py\", \"purpose\": "
    "\"unit-test <module_from_spec>\", \"api\": []}, "
    "{\"path\": \"pyproject.toml\", \"purpose\": \"build manifest\", \"api\": []}]}"
)


def _architect_context(spec: str, cwd: str | None) -> str:
    """SKILLS / WORKFLOWS / REPO RULES for the architect — via the SHARED
    context bundle so the rule source (repo_rules + md_store, query-gated)
    matches every other path. Was `repo_rules.collect` = a divergent rule
    source. Each block capped ~1000 chars."""
    from aiforge_core.runtime import context_bundle as _cb
    b = _cb.build_bundle(cwd or ".", spec, want_repo_map=False,
                         want_summary=False, want_prefs=False)
    parts: list[str] = []
    if b.skills_md:
        parts.append("SKILLS:\n" + b.skills_md.strip()[:1000])
    if b.workflows_md:
        parts.append("WORKFLOWS:\n" + b.workflows_md.strip()[:1000])
    if b.rules_md:
        parts.append("REPO RULES:\n" + b.rules_md.strip()[:1000])
    return "\n\n".join(parts)


class _ArchFileSpec(BaseModel):
    path: str
    purpose: str = ""
    api: list[str] = []


class _ArchitectPlan(BaseModel):
    files: list[_ArchFileSpec] = []


def _ask_architect(msg: str) -> list[dict]:
    from aiforge_core.llm.structured import structured_complete
    plan = structured_complete("architect", [
        {"role": "system", "content": _ARCHITECT_SYS},
        {"role": "user", "content": msg}],
        _ArchitectPlan, max_tokens=4000, timeout_s=_orchestrator_timeout_s())
    return [{"path": f.path, "purpose": f.purpose, "api": f.api}
            for f in plan.files if (f.path or "").strip()]


def _reask_plan(user_msg: str, files: list[dict],
                issues: list[str]) -> tuple[list[dict], list[str]]:
    """PLAN GATE: the architect is a single point of failure — give the model
    ONE semantic reask naming the exact defects. Hard defects (dupes, escapes)
    are sanitized either way; a still-broken retry ships the sanitized plan with
    a warning rather than stalling the run."""
    log.warning("architect plan issues (reasking once): %s", issues)
    retry, retry_issues = _validate_plan(_ask_architect(
        user_msg + "\n\nYOUR PREVIOUS PLAN HAD DEFECTS — produce a "
        "corrected plan fixing EVERY one of these:\n- " + "\n- ".join(issues)))
    if retry and len(retry_issues) < len(issues):
        files, issues = retry, retry_issues
    if issues:
        log.warning("architect plan still imperfect after reask "
                    "(shipping sanitized): %s", issues)
    return files, issues


def _hard_cap_modules(files: list[dict]) -> list[dict]:
    """The re-ask is ADVISORY and local models routinely ignore it — so if the
    plan STILL over-fragments, coalesce the excess modules deterministically
    (plan-time, symbol-preserving) rather than fan out an uncompilable split.
    Disable with AIFORGE_ARCHITECT_HARD_CAP=0."""
    if os.environ.get("AIFORGE_ARCHITECT_HARD_CAP", "1") in ("0", "false"):
        return files
    files, removed = _coalesce_code_modules(files)
    if removed:
        log.info("architect over-fragmented past the cap — coalesced "
                 "%d module(s) deterministically (symbols preserved)", removed)
    return files


def _existing_repo_note(cwd: str | None) -> str:
    """In an EXISTING project the "always add tests + a manifest" rule is
    wrong: "fix money.py so the tests pass" planned a new pyproject.toml and a
    rewrite of the very tests the user said not to touch."""
    if not cwd:
        return ""
    try:
        from ._reconcile import _is_greenfield
        if _is_greenfield(cwd):
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return ("\n\nEXISTING PROJECT — this overrides the ALWAYS-include rule: plan "
            "ONLY the files the request changes or creates. Do NOT add a build "
            "manifest, a new test file or an integration test unless the "
            "request asks for one, and NEVER plan a file the request says not "
            "to edit (e.g. 'do not edit the tests' → no test files).")


def _architect(spec: str, *, cwd: str | None = None) -> list[dict]:
    """Orchestrator agent 2: design the file structure (disjoint files), guided
    by the repo's skills/workflows/rules. Returns [{path, purpose}, ...] — the
    single source of truth for the split. Backward compatible (cwd optional).
    Uses structured_complete (Pydantic-validated, schema-prompt + reask) —
    replaces the old lossy ``re.search(r"{.*}")`` scrape that silently
    returned [] on any malformed reply."""
    context = ""
    try:
        context = _architect_context(spec, cwd)
    except Exception as exc:  # noqa: BLE001
        log.debug("architect context gather failed: %s", exc)
    user_msg = spec + (("\n\n" + context) if context else "") \
        + _existing_repo_note(cwd)
    try:
        files, issues = _validate_plan(_ask_architect(user_msg))
        if issues:
            files, issues = _reask_plan(user_msg, files, issues)
        return _hard_cap_modules(files)
    except Exception as exc:  # noqa: BLE001
        log.warning("architect step failed: %s", exc)
        return []


def _module_contract(files: list[dict]) -> str:
    """A shared symbol→module map injected into EVERY subtask's brief.

    The #1 parallel-decompose failure on a local model: each subtask builds in an
    isolated worktree knowing only ITS own file's api, so one subtask's
    ``__init__.py`` writes ``from .queue import TaskQueue`` while the subtask that
    actually defines ``TaskQueue`` put it in ``core.py`` — the imports don't line
    up and the reconciled package won't even import. Pinning WHERE each shared
    symbol lives removes the guess: a subtask importing a symbol reads this map
    and uses the EXACT module that defines it. Tests + api-less files are omitted
    (nothing imports from them)."""
    lines: list[str] = []
    for f in files:
        path = str(f.get("path") or "").strip().lstrip("/")
        api = [str(a) for a in (f.get("api") or []) if a]
        if not path or path.startswith("tests/") or not api:
            continue
        lines.append(f"- `{path}` defines: " + "; ".join(api))
    if len(lines) < 2:
        return ""            # nothing cross-module to coordinate
    return ("PROJECT MODULE MAP — when you import a symbol another module owns, "
            "import it from EXACTLY the module named below; NEVER invent a module "
            "name or assume a symbol lives in a differently-named file:\n"
            + "\n".join(lines))


def _plan_files(files: list[dict]) -> list[dict]:
    """Architect file list → one subtask per file (guaranteed distinct files).

    The slug must be UNIQUE within the plan: it names the worktree dir + branch,
    so two files sharing a basename (``a/db.py`` + ``b/db.py``) slugging to the
    same ``db`` would collide on one worktree → two workers clobber each other.
    On a slug collision we disambiguate with a short hash of the FULL path.

    Every subtask's goal also carries the shared MODULE MAP (:func:`_module_contract`)
    so isolated worktrees can't diverge on where a shared symbol lives — the
    parallel-decompose cohesion fix."""
    import hashlib
    contract = _module_contract(files)
    out, seen_paths, seen_slugs = [], set(), set()
    for f in files:
        path = str(f.get("path") or "").strip().lstrip("/")
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        slug = _slugify(path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or path)
        if slug in seen_slugs:
            # Same basename as an earlier file — append a short stable hash of
            # the full path so the worktree dir/branch stays unique.
            suffix = hashlib.sha1(path.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
            slug = f"{slug}-{suffix}"
        seen_slugs.add(slug)
        _api = [str(a) for a in (f.get("api") or []) if a]
        out.append({"slug": slug, "path": path, "api": _api,
                    "goal": f"{path}: {f.get('purpose') or 'implement'}"
                            + (" | MUST expose EXACTLY: " + "; ".join(_api) if _api else "")
                            + (("\n\n" + contract) if contract else "")})
    return out


def _decompose(prompt: str, tries: int = 2) -> list[dict]:
    """Planner LLM call → subtasks list (JSON array or markdown phases).
    Retries once: a single shot occasionally returns an unparseable format on a
    local model, so we try again before giving up."""
    from aiforge_core.runtime.subtasks_callback import _extract_subtickets
    for attempt in range(max(1, tries)):
        try:
            from aiforge_core.llm import client
            out = client.complete("planner", [
                {"role": "system", "content": _DECOMPOSE_SYS},
                {"role": "user", "content": prompt}], max_tokens=1500,
                timeout_s=_orchestrator_timeout_s())
            subs = _extract_subtickets(out)
            if len(subs) >= 2:
                return subs
        except Exception as exc:  # noqa: BLE001
            log.warning("parallel decompose attempt %d failed: %s", attempt, exc)
    return []


def _ensure_git_workspace(cwd: str) -> str:
    """Make ``cwd`` a git repo with a committed baseline so worktrees can branch
    off it. Returns the base branch name."""
    os.makedirs(cwd, exist_ok=True)
    existing = _git(["rev-parse", "--git-dir"], cwd).returncode == 0
    if not existing:
        _git(["init"], cwd)
        _git(["config", "user.email", "aiforge@local"], cwd)
        _git(["config", "user.name", "aiforge"], cwd)
    if existing and not _is_managed_workspace(cwd):
        # The user's own repo: their .gitignore is theirs — keep the agent's
        # artifacts out through .git/info/exclude instead.
        from aiforge_core.runtime.team_workspace import ensure_exclude
        ensure_exclude(cwd)
    else:
        # A fresh workspace is born with the agent's own artifacts gitignored.
        ensure_artifact_gitignore(cwd)
    # need at least one commit for `worktree add <base>` to resolve
    if _git(["rev-parse", "HEAD"], cwd).returncode != 0:
        readme = os.path.join(cwd, ".aiforge-workspace")
        if not os.path.exists(readme):
            with open(readme, "w") as f:
                f.write("aiforge chat workspace\n")
        # .gitignore is the committed baseline (the workspace marker is
        # excluded); excludes keep any stray junk out of the baseline too.
        _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], cwd)
        _git(["commit", "-m", "workspace baseline"], cwd)
    cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return (cur.stdout or "").strip() or "main"


def _is_managed_workspace(cwd: str) -> bool:
    """True only when ``cwd`` is an AIForge-OWNED throwaway workspace — a chat
    session dir (…/chat-workspaces/session-N) or a ticket worktree
    (…/.aiforge-worktrees/…). A user-pinned real project is NOT managed and must
    never have its working tree auto-committed."""
    try:
        p = os.path.realpath(cwd) + os.sep
    except Exception:  # noqa: BLE001
        return False
    return (
        (os.sep + "chat-workspaces" + os.sep + "session-") in p
        or (os.sep + ".aiforge-worktrees" + os.sep) in p
    )


def _commit_turn_baseline(cwd: str) -> str:
    """Ensure ``cwd`` is a git repo and return a HEAD sha to diff this turn
    against. For an AIForge-MANAGED workspace we also commit the current tree so
    a reused workspace's leftover files (a previous task's edits) fold into the
    baseline instead of being mistaken for THIS turn's work. For a USER-PINNED
    repo we do NOT touch the index/history — staging + committing the user's
    uncommitted WIP onto their branch every turn is destructive; we just read
    HEAD and let the working-tree diff show their changes as before. Returns ''
    only if git is entirely unusable."""
    try:
        _ensure_git_workspace(cwd)
        if _is_managed_workspace(cwd):
            # gitignore keeps artifacts out; --allow-empty just pins HEAD.
            _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], cwd)
            _git(["commit", "--allow-empty", "-q", "-m", "pre-turn baseline"], cwd)
        return (_git(["rev-parse", "HEAD"], cwd).stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""

# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._worktree import _git, _slugify, log
