from __future__ import annotations

import re


def _trial_run_script(path: str, jobs_scripts) -> dict:
    """TEST BEFORE SCHEDULE: run the script once. A wrong JQL/filter would
    otherwise be scheduled as-is and fire forever doing nothing. On failure the
    orphan script is deleted too, so a rejected build leaves nothing behind.

    ``{"ok": True, "stdout": …}`` when it ran, otherwise the tool's error dict.
    """
    trial = jobs_scripts.run_script(path)
    if trial.get("ok"):
        return {"ok": True, "stdout": trial.get("stdout")}
    jobs_scripts.delete_script(path)
    return {"ok": False, "tested": True,
            "error": ("trial run FAILED (exit "
                      f"{trial.get('returncode')}) — job NOT "
                      "scheduled. Fix the script and retry.\n"
                      f"STDOUT:\n{trial.get('stdout', '')}\n"
                      f"STDERR:\n{trial.get('stderr', '')}")}


def _replace_same_named_jobs(name: str, path: str, jobs_store,
                             jobs_scripts) -> list:
    """DEDUPE: drop any existing job(s) with the same name (and their script
    files) instead of piling up duplicates that all fire. Best-effort — a
    dedupe failure never blocks the create."""
    replaced = []
    try:
        for j in jobs_store.list_jobs():
            if str(j.get("name") or "").strip().lower() != name.lower():
                continue
            sp = j.get("script_path")
            if sp and sp != path and jobs_scripts.is_within_jobs_dir(sp):
                jobs_scripts.delete_script(sp)
            jobs_store.delete(j["id"])
            replaced.append(j["id"])
    except Exception:  # noqa: BLE001
        pass
    return replaced


def _t_create_job_script(args: dict, _cwd: str) -> dict:
    """JOB-BUILDER finalize: write the approved script to the local
    ~/.aiforge/jobs folder and register a cron job that RUNS it (deterministic
    — no ticket, no LLM per fire). Args: name, cron, script, optional
    description. Mirrors POST /api/jobs/script so the chat builder can finalize
    in-conversation."""
    try:
        name = str(args.get("name") or "").strip()
        cron = str(args.get("cron") or "").strip()
        script = str(args.get("script") or "")
        if not name or not cron or not script.strip():
            return {"ok": False, "error": "need name, cron, and script"}
        from aiforge_core.jobs import parse as jobs_parse
        from aiforge_core.jobs import scripts as jobs_scripts
        from aiforge_core.jobs import store as jobs_store
        if not jobs_parse.schedulable(cron):
            return {"ok": False,
                    "error": f"invalid or unschedulable cron: {cron!r}"}
        path = jobs_scripts.write_script(name, script)
        # `skip_test` (default off) is the escape for destructive or
        # time-sensitive scripts.
        tested = not bool(args.get("skip_test"))
        trial_output = None
        if tested:
            trial = _trial_run_script(path, jobs_scripts)
            if not trial["ok"]:
                return trial
            trial_output = trial["stdout"]
        replaced = _replace_same_named_jobs(name, path, jobs_store,
                                            jobs_scripts)
        job = jobs_store.create(
            name=name, cron=cron, ticket_title=name,
            ticket_body=(str(args.get("description") or "").strip()
                         or f"Runs script: {path}"),
            next_run_at=jobs_parse.next_runs(cron, n=1)[0],
            kind="script", script_path=path)
        return {"ok": True, "job_id": job["id"], "script_path": path,
                "human_schedule": jobs_parse.human_schedule(cron),
                "next_run_at": job["next_run_at"],
                "tested": tested,
                "trial_output": trial_output,
                "replaced_jobs": replaced}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_ensure_runtime(args: dict, _cwd: str) -> dict:
    """Install + verify missing language runtimes / build tools so the
    agent can actually build & run the project."""
    try:
        from aiforge_core.runtime.tools.ensure_runtime import ensure_runtime
        tools = args.get("tools") or args.get("tool") or []
        return ensure_runtime(tools)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_project(args: dict, cwd: str) -> dict:
    """Detect/install/build/test/run any common stack (maven, gradle,
    node/react/next/vite, python, go, rust) with the canonical command."""
    try:
        from aiforge_core.runtime.tools.project_runner import project
        return project(action=args.get("action", "detect"),
                       cwd=args.get("cwd") or cwd,
                       timeout=int(args.get("timeout", 1800)))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_set_repo_folder(args: dict, _cwd: str) -> dict:
    """Persist the local FOLDER for a repo so tickets/pipeline runs for that
    repo resolve to it — ``repo`` = the project name, ``path`` = its absolute
    local folder. Use when the user says 'use /x/y for repo foo' or 'repo foo
    lives at /x/y'. Stored in repos.json; read by the workspace resolver."""
    from aiforge_core.config import repo_map
    repo = str(args.get("repo") or "").strip()
    path = str(args.get("path") or "").strip()
    if not repo or not path:
        return {"ok": False, "error": "need repo and path"}
    import os as _os
    if not _os.path.isdir(_os.path.expanduser(path)):
        return {"ok": False, "error": f"not a directory: {path}"}
    return repo_map.set_path(repo, path)


def _t_set_repo_root(args: dict, _cwd: str) -> dict:
    """Persist the GLOBAL base folder that holds all repos — ``path`` = the
    directory whose subfolders are repos (a ticket for project ``foo`` resolves
    to ``<path>/foo``). Use when the user says 'all repos live under /x' or
    'the global repo folder is /x'."""
    from aiforge_core.config import repo_map
    path = str(args.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "need path"}
    import os as _os
    if not _os.path.isdir(_os.path.expanduser(path)):
        return {"ok": False, "error": f"not a directory: {path}"}
    return repo_map.set_default_root(path)


def _t_list_repos(_args: dict, _cwd: str) -> dict:
    """List the configured repo folders: the global base + explicit per-repo
    paths + the git repos found under the base."""
    from aiforge_core.config import repo_map
    import os as _os
    cfg = repo_map.list_all()
    root = cfg["default_root"]
    found = []
    try:
        for d in sorted(_os.listdir(root)):
            p = _os.path.join(root, d)
            if _os.path.isdir(_os.path.join(p, ".git")):
                found.append(d)
    except OSError:
        pass
    return {"ok": True, "default_root": root, "paths": cfg["paths"],
            "repos_under_root": found}


def _t_set_integration_default(args: dict, _cwd: str) -> dict:
    """Persist a user-stated DEFAULT so later tool calls auto-fill it —
    ``tool`` = jira | confluence, ``value`` = the project key (jira) or space
    key (confluence). Deterministic: stored in the integrations config, read by
    jira_*/confluence_* on every call. Use when the user says e.g. 'use ENG as
    the default project' / 'default Confluence space is DEV'."""
    tool = str(args.get("tool") or "").strip().lower()
    value = str(args.get("value") or "").strip()
    if tool not in ("jira", "confluence"):
        return {"ok": False, "error": "tool must be 'jira' or 'confluence'"}
    if not value:
        return {"ok": False, "error": "missing 'value' (project/space key)"}
    field = "default_project" if tool == "jira" else "default_space"
    try:
        from aiforge_core.config import integrations
        integrations.set_(tool, {field: value})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "tool": tool, field: value,
            "note": f"{tool} calls will now default {field}={value} when omitted"}


def _t_resolve_repo(args: dict, _cwd: str) -> dict:
    """Resolve a loosely-typed repo/service/folder name to its local path
    (tolerates case, spaces, missing hyphens, typos)."""
    from aiforge_core.config import repo_map
    return repo_map.resolve(args.get("name") or args.get("repo") or "")


def _t_context_gather(args: dict, _cwd: str) -> dict:
    """Assemble a cross-entity dossier (a Jira ticket + its linked Confluence
    pages + images, or vice versa) in PARALLEL, cache it in the context folder,
    and refresh only when the entity changed. Use when asked to explain/
    understand a ticket or page."""
    from aiforge_core.runtime import context_gather as _cg
    kind = (args.get("kind") or "").lower()
    key = str(args.get("key") or args.get("id") or "").strip()
    if not kind and key:
        # infer: a JIRA-KEY looks like PROJ-42 (case-insensitive); else a
        # numeric id → confluence. Normalize a jira key to uppercase.
        if re.match(r"^[A-Za-z][A-Za-z0-9]+-\d+$", key):
            kind, key = "jira", key.upper()
        else:
            kind = "confluence"
    if kind not in ("jira", "confluence") or not key:
        return {"ok": False, "error": "need kind (jira|confluence) + key/id"}
    return _cg.gather(kind, key, force=bool(args.get("force")),
                      role="chat")


def _t_note_curate(args: dict, cwd: str) -> dict:
    """Re-verify a managed workspace note (ticket.md/page.md/dossier note):
    re-fetch the source, refresh drifted Facts, flag dead links, and log each
    change under ## Learnings. Path defaults to the bound context's note.
    NOT in _READONLY_TOOLS — it WRITES the note; it stays ungated (ALLOW)
    because the curator's own path jail confines writes to the managed
    work root (see note_curator)."""
    from aiforge_core.runtime import note_curator
    path = str(args.get("path") or "").strip()
    if not path:
        path = note_curator.primary_note_for_cwd(cwd) or ""
    if not path:
        return {"ok": False,
                "error": "no managed note found — pass 'path' or run inside "
                         "a jira/confluence context workspace"}
    return note_curator.curate_note(path, cwd=cwd)


def _t_note_consolidate(args: dict, cwd: str) -> dict:
    """Intelligently fold NEW knowledge into a managed note's OKR sections:
    an LLM dedupes paraphrases, resolves contradictions, and MAPS each item to
    the right section (Objective/Key Results/Facts/Links/Learnings); large input
    is chunked on structure boundaries. Path defaults to the bound context's
    note. WRITES — jailed to the managed work root (same boundary as
    note_curate), so it stays ungated."""
    from aiforge_core.runtime import note_curator, work_notes
    text = str(args.get("text") or args.get("content") or "").strip()
    if not text:
        return {"ok": False, "error": "pass 'text' — the new knowledge to fold "
                                      "into the note"}
    path = str(args.get("path") or "").strip()
    if not path:
        path = note_curator.primary_note_for_cwd(cwd) or ""
    if not path:
        return {"ok": False,
                "error": "no managed note found — pass 'path' or run inside "
                         "a jira/confluence context workspace"}
    if not note_curator._inside_work_root(path):
        return {"ok": False,
                "error": "path outside the managed work root — refusing"}
    return work_notes.consolidate_note(path, text, role="learner")


def _t_email_send(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import email_tool
    return email_tool.email_send(args, cwd)


def _t_email_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import email_tool
    return email_tool.email_read(args, cwd)


def _t_serve(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import serve
    return serve.serve(args, cwd)


def _t_stop_service(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import serve
    return serve.stop_service(args, cwd)


def _t_list_services(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import serve
    return serve.list_services(args, cwd)
