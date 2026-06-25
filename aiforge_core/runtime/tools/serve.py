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
    return (os.environ.get("AIFORGE_WORKSPACE_DIR") or cwd
            or os.environ.get("AIFORGE_REPO_ROOT") or os.getcwd())


def serve(args: dict, cwd: str | None = None) -> dict:
    """Start ``cmd`` as a background service. Polls its early output for up to
    ``wait_s`` (default 12) to detect the bound URL/port, then returns without
    waiting for the (long-lived) process to exit. ``port`` is an optional hint
    used to build the URL when the log doesn't print one."""
    cmd = (args.get("cmd") or "").strip()
    if not cmd:
        return {"ok": False, "error": "missing 'cmd'"}
    base = _root(args.get("cwd") or cwd)
    try:
        wait_s = float(args.get("wait_s", 12))
    except (TypeError, ValueError):
        wait_s = 12.0
    try:
        ttl = float(args["ttl_s"]) if args.get("ttl_s") is not None \
            else _default_ttl()
    except (TypeError, ValueError):
        ttl = _default_ttl()
    port_hint = str(args.get("port") or "").strip()
    _ensure_reaper()        # auto-cleanup forgotten services
    _reap()                 # opportunistically clear dead/expired first

    log_path = os.path.join(
        os.environ.get("AIFORGE_CONFIG_DIR", os.path.expanduser("~/.aiforge")),
        f"serve-{abs(hash(cmd)) % 100000}.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        logf = open(log_path, "w+")
    except Exception:  # noqa: BLE001
        logf = subprocess.PIPE  # type: ignore
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=base, text=True,
            stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    # Register + bind to the session so the Stop button can also kill it.
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

    # Watch the log for a URL/port (or an early crash) for up to wait_s.
    url = None
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:        # died on startup
            tail = _read_log(log_path)
            _SERVICES.pop(proc.pid, None)
            return {"ok": False, "error": "service exited on startup "
                    f"(code {proc.returncode})", "log_tail": tail[-1500:]}
        text = _read_log(log_path)
        m = _URL_RE.search(text)
        if m:
            url = m.group(0).replace("0.0.0.0", "localhost")
            break
        if not port_hint:
            pm = _PORT_RE.search(text)
            if pm:
                port_hint = pm.group(1)
        time.sleep(0.4)

    if not url and port_hint:
        url = f"http://localhost:{port_hint}"
    if proc.pid in _SERVICES:
        _SERVICES[proc.pid]["url"] = url
        _SERVICES[proc.pid]["port"] = port_hint or None
    ttl_note = (f" · auto-stops after {int(ttl // 60)} min if you forget"
                if ttl > 0 else "")
    return {"ok": True, "pid": proc.pid, "url": url,
            "port": port_hint or None, "log": log_path, "cmd": cmd,
            "ttl_s": ttl,
            "hint": (f"running — open {url}" if url else
                     "running (no URL detected; check the log)")
                    + f" · stop with stop_service(pid={proc.pid}){ttl_note}"}


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
