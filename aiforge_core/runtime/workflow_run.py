"""Run a saved workflow's script as ONE step.

A deploy, a pipeline check, a release: the steps are the same every time, so
they are kept as a script next to the workflow's ``WORKFLOW.md`` and run by
the harness, not re-derived by the model call by call. The model makes one
call — ``workflow_run {"name": ...}`` — and gets one result. Only when the
script fails does it get the output and the written procedure, to take over
from the step that broke.

This module only decides WHAT to run (which workflow, which script, which
command line). The command itself goes through the caller's command runner,
so approval, the sandbox, time limits and the action log are the ones every
other command gets.
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Callable

#: Script names tried, in order, when the call names none and there are several.
_ENTRY_STEMS = ("run", "main", "workflow")
_BODY_MAX = 3000
DONE_NOTE = ("[workflow '{name}' finished: every step of it ran and exited 0. "
             "Do not run it again and do not redo or re-check its steps — "
             "report this output as the result.]")


def _slug(name: str) -> str:
    from aiforge_core.runtime import skills as _sk
    return _sk._slug(name or "")


def resolve(name: str, cwd: "str | None"):
    """The workflow called ``name`` (exact, or the same slug), or None."""
    from aiforge_core.runtime import workflows
    want, slug = (name or "").strip().lower(), _slug(name)
    for wf in workflows.load(cwd):
        if wf.name.strip().lower() == want or _slug(wf.name) == slug:
            return wf
    return None


def pick_script(paths: list, script: str = "") -> "tuple[str | None, str]":
    """``(path, error)``: the script to run. The one the call names; else the
    only one; else the one called run / main / workflow."""
    if not paths:
        return None, "no script"
    by_name = {Path(p).name: p for p in paths}
    if script:
        hit = by_name.get(Path(script).name)
        return (hit, "") if hit else (
            None, f"no script {script!r}; it has: {', '.join(sorted(by_name))}")
    if len(paths) == 1:
        return paths[0], ""
    for stem in _ENTRY_STEMS:
        for fname, path in sorted(by_name.items()):
            if Path(fname).stem.lower() == stem:
                return path, ""
    return None, ("several scripts and none is called run.*: pass "
                  f"\"script\" — one of {', '.join(sorted(by_name))}")


def command(path: str, args=None) -> str:
    """The shell line that runs ``path`` with ``args`` (a list, or one string
    split like a shell would). Every argument is quoted."""
    from aiforge_core.runtime._workflow_scripts import _SCRIPT_RUNNER_BY_EXT
    if isinstance(args, str):
        args = shlex.split(args)
    words = [shlex.quote(str(a)) for a in (args or [])]
    runner = _SCRIPT_RUNNER_BY_EXT.get(Path(path).suffix.lower())
    head = [runner] if runner else []
    return " ".join(head + [shlex.quote(path)] + words)


def script_lines(args: "dict | None", cwd: "str | None" = None) -> list:
    """The command lines of the script a ``workflow_run`` call would run, for
    the gates that judge a command before it runs (risk, egress): a runbook
    gets the verdict its lines would get one by one. Empty when the call
    resolves to no script, or the script is not a shell script (its own
    interpreter line is then the only thing a shell sees)."""
    from aiforge_core.runtime import workflows
    if not isinstance(args, dict):
        return []
    if cwd is None:
        try:
            from aiforge_core.runtime.chat_agent._shell import _workspace_root
            root = _workspace_root()
            cwd = str(root) if root is not None else None
        except Exception:  # noqa: BLE001
            cwd = None
    try:
        wf = resolve(str(args.get("name") or args.get("workflow") or ""), cwd)
        if wf is None:
            return []
        path, _why = pick_script(
            workflows.scripts_for(getattr(wf, "source", "") or ""),
            str(args.get("script") or ""))
        if path is None:
            return []
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — a gate helper never raises
        return []
    lines = [ln.strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")][:400]


def _names(cwd, query: str) -> list:
    from aiforge_core.runtime import workflows
    try:
        return [h.get("name") for h in workflows.search(query, cwd, k=5)
                if h.get("name")]
    except Exception:  # noqa: BLE001
        return []


def run(name: str, cwd: str, runner: Callable[[dict, str], dict], *,
        args=None, script: str = "", timeout=None, background=False) -> dict:
    """Run workflow ``name``'s script through ``runner`` (the command tool).

    Returns the command's own result plus ``workflow`` / ``script``. A failure
    also carries the written procedure, so the model can continue by hand."""
    from aiforge_core.runtime import workflows
    wf = resolve(name, cwd)
    if wf is None:
        near = _names(cwd, name)
        return {"ok": False, "error": f"no workflow named {name!r}"
                + (f"; closest: {', '.join(near)}" if near else "")}
    body = (wf.body or "")[:_BODY_MAX]
    path, why = pick_script(workflows.scripts_for(getattr(wf, "source", "") or ""),
                            script)
    if path is None:
        if why == "no script":
            return {"ok": False, "workflow": wf.name, "no_script": True,
                    "steps": body,
                    "hint": "This workflow has no script yet: follow its steps "
                            "now. When they have worked, save them with "
                            "learn_workflow (scripts: run.sh) so that next "
                            "time it is one workflow_run call."}
        return {"ok": False, "workflow": wf.name, "error": why}
    call = {"cmd": command(path, args)}
    if timeout:
        call["timeout"] = timeout
    if background:
        call["background"] = True
    res = dict(runner(call, cwd) or {})
    res.update(workflow=wf.name, script=Path(path).name)
    failed = res.get("ok") is False or (isinstance(res.get("code"), int)
                                        and res["code"] != 0)
    if not failed and not res.get("running") and isinstance(res.get("code"), int):
        # Said in the output itself, where the model reads the result: live,
        # with nothing saying so, it ran the same deploy a second time.
        res["stdout"] = (str(res.get("stdout") or "").rstrip("\n") + "\n" + DONE_NOTE.format(name=wf.name))
    if failed:
        res["ok"] = False
        res["steps"] = body
        res["hint"] = ("The script stopped with an error. Read its output, fix "
                       "the cause or do the remaining steps by hand; do not "
                       "re-run the whole script blindly.")
    return res


def succeeded_in(steps) -> bool:
    """A ``workflow_run`` in these tool steps ran to the end and exited 0."""
    for s in steps or []:
        res = s.get("result") if isinstance(s, dict) else None
        if (isinstance(s, dict) and s.get("name") == "workflow_run"
                and isinstance(res, dict) and res.get("code") == 0
                and res.get("ok") is not False):
            return True
    return False


__all__ = ["DONE_NOTE", "command", "pick_script", "resolve", "run",
           "script_lines", "succeeded_in"]
