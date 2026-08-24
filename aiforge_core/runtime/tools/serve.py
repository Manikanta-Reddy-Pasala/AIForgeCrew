"""Background app runner — start a long-lived process (a dev server / API),
detect the URL it binds, and return immediately with a PID the operator can
stop. Closes the "run it and give me the endpoint, then let me stop it" gap:
``run_command``/``project run`` BLOCK (a server runs forever), so they can't
start a service and continue.

Tools (registered in the chat agent):
  serve(cmd, cwd?, port?, wait_s?)  → {ok, pid, url, port, log, cmd}
  stop_service(pid)                 → {ok, stopped, pid}
  list_services()                   → {ok, services: [{pid, url, cmd, alive}]}

Each started service is tracked per-process so the chat Stop button (via
chat_cancel) AND an explicit stop_service both kill the whole process group.
"""
from __future__ import annotations

import atexit
import os
import re
import signal
import subprocess
import threading
import time

# pid → {proc, cmd, url, port, log_path, pgid, started_at, ttl}
_SERVICES: dict[int, dict] = {}
_REAPER_STARTED = False
_REAPER_LOCK = threading.Lock()


def _default_ttl() -> float:
    """Max lifetime (seconds) for a served process before auto-cleanup, so a
    forgotten dev server doesn't linger forever. 0 disables. Default 30 min."""
    try:
        return float(os.environ.get("AIFORGE_SERVE_TTL_S", "1800"))
    except ValueError:
        return 1800.0


def _kill_pgid(pid: int, pgid: int | None) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid if pgid is not None else os.getpgid(pid), sig)
            time.sleep(0.2)
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, sig)
            except Exception:  # noqa: BLE001
                pass


def _reap() -> int:
    """Kill services past their TTL or already dead. Returns count reaped."""
    now = time.monotonic()
    reaped = 0
    for pid, s in list(_SERVICES.items()):
        dead = s["proc"].poll() is not None
        ttl = s.get("ttl") or 0
        expired = ttl > 0 and (now - s.get("started_at", now)) > ttl
        if dead:
            _SERVICES.pop(pid, None)
        elif expired:
            _kill_pgid(pid, s.get("pgid"))
            _SERVICES.pop(pid, None)
            reaped += 1
    return reaped


def _ensure_reaper() -> None:
    """Start a single daemon thread that reaps expired services every 60s, so
    a forgotten server is cleaned up even with no further tool calls."""
    global _REAPER_STARTED
    with _REAPER_LOCK:
        if _REAPER_STARTED:
            return
        _REAPER_STARTED = True

        def _loop() -> None:
            while True:
                time.sleep(60)
                try:
                    _reap()
                except Exception:  # noqa: BLE001
                    pass

        t = threading.Thread(target=_loop, name="aiforge-serve-reaper",
                             daemon=True)
        t.start()


@atexit.register
def _stop_all_on_exit() -> None:
    """Kill every still-running served process when the host process exits, so
    none are orphaned on shutdown/restart."""
    for pid, s in list(_SERVICES.items()):
        if s["proc"].poll() is None:
            _kill_pgid(pid, s.get("pgid"))
        _SERVICES.pop(pid, None)

# Common "I'm listening" lines emitted by frameworks on startup.
_URL_RE = re.compile(r"https?://[\w.\-]+:\d+(?:/\S*)?", re.IGNORECASE)
_PORT_RE = re.compile(
    r"(?:listening|running|started|serving|local|port)[^\d]{0,20}?(\d{2,5})",
    re.IGNORECASE)


def _root(cwd: str | None) -> str:
    from aiforge_core.runtime import request_context
    return (request_context.get_workspace_dir() or cwd
            or request_context.get_repo_root() or os.getcwd())


def _delete_refusal(cmd: str, args: dict) -> dict | None:
    """Destructive-delete backstop: serve runs ``cmd`` via a shell, so it must
    honour the same delete gate as run_command/bash — otherwise an `rm -rf` (or
    any destructive form) smuggled in as a "server" command runs with no
    confirmation. Normal dev-server commands (npm run dev, etc) pass."""
    from aiforge_core.runtime.tools import delete_guard
    confirmed = bool(args.get("confirm_delete")) or delete_guard.allow_delete(
        ("AIFORGE_CHAT_ALLOW_DELETE", "AIFORGE_ALLOW_DELETE"))
    if not confirmed and delete_guard.is_destructive_delete(cmd):
        return {"ok": False, "error": delete_guard.REFUSAL
                + " (re-issue with confirm_delete=true once the user agrees)"}
    return None


def _float_arg(args: dict, key: str, default: float) -> float:
    try:
        raw = args.get(key)
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _open_service_log() -> tuple[str, object]:
    """``(path, file or PIPE)``. The name is unique per invocation — a
    hash(cmd) name collides (two different cmds, or the same cmd served twice)
    and the URL/port detection then reads the wrong service's log."""
    import uuid as _uuid
    from aiforge_core.config.paths import config_dir
    log_path = os.path.join(str(config_dir()),
                            f"serve-{_uuid.uuid4().hex[:10]}.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        return log_path, open(log_path, "w+")
    except Exception:  # noqa: BLE001
        return log_path, subprocess.PIPE


def _register_service(proc, cmd: str, port_hint: str, log_path: str,
                      ttl: float) -> None:
    """Track the service and bind it to the session so Stop can kill it too."""
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:  # noqa: BLE001
        pgid = None
    _SERVICES[proc.pid] = {"proc": proc, "cmd": cmd, "url": None,
                           "port": port_hint or None, "log_path": log_path,
                           "pgid": pgid, "started_at": time.monotonic(),
                           "ttl": ttl}
    try:
        from aiforge_core.runtime import chat_cancel
        sid = chat_cancel.active()
        if sid is not None and pgid is not None:
            chat_cancel.track_pgid(sid, pgid)
    except Exception:  # noqa: BLE001
        pass


def _await_url(proc, log_path: str, port_hint: str,
               wait_s: float) -> tuple[str | None, str, dict | None]:
    """Watch the log for a URL/port (or an early crash) for up to ``wait_s``.
    Returns ``(url, port_hint, early_exit_result)``."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:        # died on startup
            tail = _read_log(log_path)
            _SERVICES.pop(proc.pid, None)
            return None, port_hint, {
                "ok": False,
                "error": f"service exited on startup (code {proc.returncode})",
                "log_tail": tail[-1500:]}
        text = _read_log(log_path)
        m = _URL_RE.search(text)
        if m:
            return m.group(0).replace("0.0.0.0", "localhost"), port_hint, None
        if not port_hint:
            pm = _PORT_RE.search(text)
            if pm:
                port_hint = pm.group(1)
        time.sleep(0.4)
    return None, port_hint, None


def _spawn_service(cmd: str, base: str):
    """``(log_path, proc, error)``. A Popen failure must not leak the opened
    log file handle."""
    log_path, logf = _open_service_log()
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=base, text=True,
                                stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True)
        return log_path, proc, ""
    except Exception as exc:  # noqa: BLE001
        try:
            if hasattr(logf, "close"):
                logf.close()
        except Exception:  # noqa: BLE001
            pass
        return log_path, None, str(exc)


def _serve_result(proc, cmd: str, url, port_hint: str, log_path: str,
                  ttl: float) -> dict:
    ttl_note = (f" · auto-stops after {int(ttl // 60)} min if you forget"
                if ttl > 0 else "")
    return {"ok": True, "pid": proc.pid, "url": url,
            "port": port_hint or None, "log": log_path, "cmd": cmd,
            "ttl_s": ttl,
            "hint": (f"running — open {url}" if url else
                     "running (no URL detected; check the log)")
                    + f" · stop with stop_service(pid={proc.pid}){ttl_note}"}


def serve(args: dict, cwd: str | None = None) -> dict:
    """Start ``cmd`` as a background service. Polls its early output for up to
    ``wait_s`` (default 12) to detect the bound URL/port, then returns without
    waiting for the (long-lived) process to exit. ``port`` is an optional hint
    used to build the URL when the log doesn't print one."""
    cmd = (args.get("cmd") or "").strip()
    if not cmd:
        return {"ok": False, "error": "missing 'cmd'"}
    refusal = _delete_refusal(cmd, args)
    if refusal is not None:
        return refusal
    base = _root(args.get("cwd") or cwd)
    wait_s = _float_arg(args, "wait_s", 12.0)
    ttl = _float_arg(args, "ttl_s", _default_ttl())
    port_hint = str(args.get("port") or "").strip()
    _ensure_reaper()        # auto-cleanup forgotten services
    _reap()                 # opportunistically clear dead/expired first

    log_path, proc, err = _spawn_service(cmd, base)
    if proc is None:
        return {"ok": False, "error": err}
    _register_service(proc, cmd, port_hint, log_path, ttl)
    url, port_hint, early = _await_url(proc, log_path, port_hint, wait_s)
    if early is not None:
        return early
    if not url and port_hint:
        url = f"http://localhost:{port_hint}"
    if proc.pid in _SERVICES:
        _SERVICES[proc.pid]["url"] = url
        _SERVICES[proc.pid]["port"] = port_hint or None
    return _serve_result(proc, cmd, url, port_hint, log_path, ttl)


def _read_log(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read()[-4000:]
    except Exception:  # noqa: BLE001
        return ""


def stop_service(args: dict, cwd: str | None = None) -> dict:
    """Stop a service started by :func:`serve`, by ``pid``. Kills the whole
    process group (TERM then KILL)."""
    try:
        pid = int(args.get("pid"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "missing/invalid 'pid'"}
    svc = _SERVICES.get(pid)
    _kill_pgid(pid, (svc or {}).get("pgid"))
    _SERVICES.pop(pid, None)
    return {"ok": True, "stopped": True, "pid": pid}


def list_services(args: dict | None = None, cwd: str | None = None) -> dict:
    """List services started this session + whether each is still alive."""
    _reap()        # drop dead/expired before listing
    out = []
    for pid, s in list(_SERVICES.items()):
        alive = s["proc"].poll() is None
        if not alive:
            _SERVICES.pop(pid, None)
        out.append({"pid": pid, "url": s.get("url"), "cmd": s.get("cmd"),
                    "alive": alive})
    return {"ok": True, "services": out}


__all__ = ["serve", "stop_service", "list_services"]
