"""command_wait / command_output / command_kill — checking on a running
command instead of blindly waiting for it (see
:mod:`aiforge_core.runtime.cmd_jobs`)."""
from __future__ import annotations

from aiforge_core.runtime import cmd_jobs


def _job_or_error(args: dict):
    ref = args.get("id") if args.get("id") not in (None, "") else args.get("pid")
    job = cmd_jobs.find(ref)
    if job is not None:
        return job, None
    known = [j.key for j in cmd_jobs.running()]
    return None, {"ok": False, "error": f"no running command with id {ref!r}",
                  "running": known,
                  "hint": ("it may have finished and been collected already"
                           if ref else "pass the id run_command returned")}


def _t_command_wait(args: dict, _cwd: str) -> dict:
    job, err = _job_or_error(args)
    if err:
        return err
    try:
        max_s = float(args.get("max_s") or 0)
    except (TypeError, ValueError):
        max_s = 0.0
    sid = None
    try:
        from aiforge_core.runtime import chat_cancel
        sid = chat_cancel.active()
    except Exception:  # noqa: BLE001
        pass
    return cmd_jobs.wait(job, max_s, session_id=sid)


def _t_command_output(args: dict, _cwd: str) -> dict:
    job, err = _job_or_error(args)
    if err:
        return err
    return cmd_jobs.look(job)


def _t_command_kill(args: dict, _cwd: str) -> dict:
    job, err = _job_or_error(args)
    if err:
        return err
    job.kill()
    res = cmd_jobs.look(job, "killed")
    res["killed"] = True
    res["hint"] = "stopped. Fix the command and run it again."
    return res


def progress_sig(name: str, args: dict) -> str:
    """Extra loop-guard key for a wait/peek: the job's progress. A repeated
    wait on a job that keeps producing output (or burning CPU) is a new call
    each time; one on a job that did nothing since is the same call again."""
    if name not in ("command_wait", "command_output"):
        return ""
    try:
        job, _ = _job_or_error(args if isinstance(args, dict) else {})
        return "|" + (job.progress_token() if job is not None else "gone")
    except Exception:  # noqa: BLE001 — the guard must never break a call
        return ""


TOOLS = {
    "command_wait": _t_command_wait,
    "command_output": _t_command_output,
    "command_kill": _t_command_kill,
}

__all__ = ["TOOLS", "progress_sig"]
