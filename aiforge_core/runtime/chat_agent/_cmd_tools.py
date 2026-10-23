"""command_wait / command_output / command_kill — checking on a running
command instead of blindly waiting for it (see
:mod:`aiforge_core.runtime.cmd_jobs`)."""
from __future__ import annotations

import os

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


_SHOWN_NOTE = ("Its log is open in the Processes panel at the bottom of the "
               "chat and updates live; the user can watch it there.")


def _chat_session():
    try:
        from aiforge_core.runtime import chat_cancel
        return chat_cancel.active()
    except Exception:  # noqa: BLE001
        return None


def _log_file(raw: str, cwd: str):
    """The file ``raw`` names, under the same rule as file_read (inside the
    workspace when one is set), or an error."""
    from ._shell import _resolve, _workspace_root
    root = _workspace_root()
    base = str(root) if root is not None else cwd
    try:
        path = str(_resolve(base, str(raw).strip()))
    except PermissionError as exc:
        return None, base, {"ok": False, "error": str(exc)}
    if not os.path.isfile(path):
        return None, base, {"ok": False, "error": f"no such file: {path}",
                            "hint": "pass the log file's path, or start the "
                                    "command that prints the log with "
                                    "run_command background:true and pass "
                                    "its id"}
    return path, base, None


def _follow_ttl_s() -> float:
    """How long a show_logs file tail runs before it stops by itself."""
    try:
        return max(60.0, float(os.environ.get("AIFORGE_SHOW_LOGS_TTL_S", 3600)))
    except (TypeError, ValueError):
        return 3600.0


def _t_show_logs(args: dict, cwd: str) -> dict:
    """Put a log in front of the user: a running (or just finished)
    command's output, or a log file followed live (``tail -F``)."""
    from aiforge_core.runtime import proc_logs
    sid = _chat_session()
    if sid is None:
        return {"ok": False, "error": "show_logs works in a chat only"}
    if args.get("path"):
        import shlex
        import time
        path, base, err = _log_file(args["path"], cwd)
        if err:
            return err
        for job in cmd_jobs.running():
            # Already following this file: show that one, no second tail.
            if getattr(job, "follow_path", None) == path \
                    and str(job.session_id) == str(sid):
                proc_logs.mark_shown(sid, job.key)
                return {"ok": True, "id": job.key, "path": path,
                        "shown": True, "reused": True, "note": _SHOWN_NOTE}
        try:
            lines = max(1, min(int(args.get("lines") or 200), 5000))
        except (TypeError, ValueError):
            lines = 200
        ttl = _follow_ttl_s()
        from ._shell import _start_background
        res = _start_background(
            f"tail -n {lines} -F {shlex.quote(path)}", base,
            deadline=time.monotonic() + ttl)
        if not res.get("id"):
            return res
        job = cmd_jobs.find(res["id"])
        if job is not None:
            job.follow_path = path
        cmd_jobs.set_label(res["id"], args.get("description")
                           or f"log: {os.path.basename(path)}")
        proc_logs.mark_shown(sid, res["id"])
        return {"ok": True, "id": res["id"], "path": path, "shown": True,
                "note": _SHOWN_NOTE + f" It follows the file for up to "
                        f"{int(ttl // 60)} min, or until stopped "
                        f"(command_kill id='{res['id']}')."}
    ref = args.get("id") if args.get("id") not in (None, "") else args.get("pid")
    if ref in (None, ""):
        shown = [j["id"] for j in proc_logs.list_for_session(sid)
                 if j["running"] and proc_logs.mark_shown(sid, j["id"])]
        if not shown:
            return {"ok": False, "error": "no command is running in this chat",
                    "hint": "start the command that prints the log with "
                            "run_command background:true, or pass a log "
                            "file's path"}
        return {"ok": True, "shown": shown, "note": _SHOWN_NOTE}
    job = cmd_jobs.find(ref)
    key = job.key if job is not None else str(ref).strip()
    if not proc_logs.mark_shown(sid, key):
        return {"ok": False, "error": f"no command with id {ref!r} in this chat",
                "running": [j.key for j in cmd_jobs.running()]}
    return {"ok": True, "id": key, "shown": True, "note": _SHOWN_NOTE}


def progress_sig(name: str, args: dict) -> str:
    """See :func:`cmd_jobs.progress_suffix`."""
    return cmd_jobs.progress_suffix(name, args)


TOOLS = {
    "command_wait": _t_command_wait,
    "command_output": _t_command_output,
    "command_kill": _t_command_kill,
    "show_logs": _t_show_logs,
}

__all__ = ["TOOLS", "progress_sig"]
