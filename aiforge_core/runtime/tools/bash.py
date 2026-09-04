"""tmux-backed persistent bash session manager for the Doer agent.

The model calls :func:`bash` which dispatches to a per-ADK-run tmux
session. If tmux is unavailable the call degrades to a stateless
subprocess that mirrors the original ``run_shell`` behaviour so the
agent loop still works on contributor boxes lacking tmux.

Lifecycle (tmux path):

* Session ``aiforge-{run_id}`` lazily created on first call.
* Custom prompt ``__AIFORGE_PROMPT_$?__`` makes exit code parsing trivial.
* Session destroyed in :func:`destroy_session` (wired from the ADK
  finish callback).
* ``restart=True`` kills + recreates the session.

Output capped at 8 KB per call; default timeout 90 s; trailing ``&``
backgrounds the job and returns immediately.
"""
from __future__ import annotations

import contextvars
import os
import re
import shutil
import subprocess
import time
from typing import Any

from aiforge_core.runtime.sandbox import root

from ._trace import emit

_STDOUT_CAP_BYTES = 8000
_DEFAULT_TIMEOUT_S = 90
_POLL_INTERVAL_S = 0.1
# Bound the post-exit communicate() drain: a daemon grandchild (`npm run dev &`,
# a spawned server) can inherit the stdout pipe and keep communicate() blocked
# FOREVER even after the main process exits. On timeout we kill the group and
# drain, returning what was captured. Env-tunable.
try:
    _COMMUNICATE_TIMEOUT_S = int(os.environ.get("AIFORGE_COMMUNICATE_TIMEOUT_S", "10"))
except ValueError:
    _COMMUNICATE_TIMEOUT_S = 10
# Per-boot random nonce in the prompt sentinel so a command whose OUTPUT happens
# to contain "__AIFORGE_PROMPT_N__" can't be mis-parsed as a shell prompt
# (wrong returncode / truncated stdout). The nonce is unpredictable, so program
# output can't accidentally forge it.
import secrets as _secrets
_NONCE = _secrets.token_hex(4)
_PROMPT_PS1 = rf"PS1='__AIFORGE_PROMPT_{_NONCE}_$?__\n'"
_SENTINEL_RE = re.compile(rf"__AIFORGE_PROMPT_{_NONCE}_(\d+)__")

_active_sessions: dict[str, str] = {}

# The run a tool call belongs to. ADK FunctionTools don't pass ``_run_id``, so
# the runner sets this to the ADK session id before the run; bash() reads it
# when no explicit id is given. This keeps ONE persistent session per run AND
# lets ``destroy_session(session.id)`` actually match it (a per-call uuid never
# would → a leaked tmux session on every bash call). Falls back to a STABLE
# "default" so even an unset run reuses one session instead of spawning many.
_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bash_run_id", default=None)


def set_run_id(run_id: str | None) -> None:
    _RUN_ID.set(run_id)


def _effective_run_id(explicit: str | None) -> str:
    return explicit or _RUN_ID.get() or "default"


def _err_result(command: str, error: str, **extra: Any) -> dict[str, Any]:
    """Error dict with the SAME key set the success path returns, so callers
    that read ``returncode``/``stdout``/``truncated`` never KeyError."""
    base = {"ok": False, "command": command, "error": error,
            "returncode": None, "stdout": "", "stderr": "", "truncated": False}
    base.update(extra)
    return base


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _session_name(run_id: str) -> str:
    return f"aiforge-{run_id}"


def _session_exists(name: str) -> bool:
    proc = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    )
    return proc.returncode == 0


def _capture(name: str) -> str:
    proc = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", name, "-S", "-10000"],
        capture_output=True,
    )
    return proc.stdout.decode("utf-8", "replace")


def _create_session(run_id: str) -> str:
    name = _session_name(run_id)
    if _session_exists(name):
        return name
    subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", name,
            "-c", str(root()), "bash", "--noprofile", "--norc",
        ],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", name, _PROMPT_PS1, "Enter"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", name, "clear", "Enter"],
        check=True, capture_output=True,
    )
    # Give the shell time to print the first prompt sentinel.
    time.sleep(0.3)
    _drain_until_prompt(name, timeout=5, expect_initial_only=True)
    _active_sessions[run_id] = name
    emit("BashSession", {"session": name, "action": "created"})
    return name


def _drain_until_prompt(
    name: str, timeout: int, expect_initial_only: bool = False,
) -> tuple[str, int | None, bool]:
    """Poll capture-pane until the sentinel reappears or timeout hits.

    Returns ``(stdout_text, returncode|None, timed_out)``. When
    ``expect_initial_only`` is True we only need to see ONE sentinel
    (used right after session create to acknowledge the first prompt).
    """
    deadline = time.monotonic() + timeout
    last_seen = ""
    try:
        from aiforge_core.runtime import chat_cancel as _cc
        _sid = _cc.active()
    except Exception:  # noqa: BLE001
        _cc, _sid = None, None
    while time.monotonic() < deadline:
        # Stop button: interrupt the running tmux command (the tmux path
        # previously ignored cancellation, so Stop did nothing until timeout).
        if _cc is not None and _sid is not None and _cc.is_cancelled(_sid):
            try:
                subprocess.run(["tmux", "send-keys", "-t", name, "C-c"],
                               capture_output=True)
            except Exception:  # noqa: BLE001
                pass
            return last_seen, -130, False   # SIGINT-style: cancelled, non-zero rc
        pane = _capture(name)
        matches = list(_SENTINEL_RE.finditer(pane))
        if expect_initial_only and len(matches) >= 1:
            return "", int(matches[-1].group(1)), False
        if len(matches) >= 2:
            second_last = matches[-2]
            last = matches[-1]
            body = pane[second_last.end() : last.start()]
            rc = int(last.group(1))
            return body.strip("\n"), rc, False
        last_seen = pane
        time.sleep(_POLL_INTERVAL_S)
    return last_seen, None, True


def _strip_echoed_command(body: str, command: str) -> str:
    r"""Drop the pane's echo of the command we just typed.

    ``tmux send-keys`` types into a pty, so the shell echoes the command back
    into the pane before its output. Everything between two prompt sentinels
    therefore starts with the command itself, and every tmux-path bash() used
    to hand the agent ``"echo $FOO\nbar"`` where the stateless path returns
    ``"bar"``. The echo can be WRAPPED across pane-width lines, so lines are
    consumed (whitespace-insensitively) until they account for the command —
    and if they never do, the body is returned untouched rather than guessing.
    """
    if not body or not command.strip():
        return body
    target = "".join(command.split())
    lines = body.split("\n")
    acc = ""
    for i, line in enumerate(lines):
        acc += "".join(line.split())
        if not target.startswith(acc):
            return body            # not an echo — keep every byte
        if acc == target:
            return "\n".join(lines[i + 1:]).strip("\n")
    return body                    # ran out of lines mid-echo: keep as-is


def _kill_group_and_reap(proc) -> tuple[bytes, bytes]:
    """SIGKILL the process group and drain it, so we don't leak pipe FDs or
    leave a zombie accumulating across a long Doer run."""
    from aiforge_core.runtime import proc_signals
    proc_signals.kill_group(proc_signals.group_of(proc))
    try:
        return proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        return b"", b""


def _completed_result(command: str, returncode, out_b, err_b) -> dict[str, Any]:
    out_s = (out_b or b"").decode("utf-8", "replace")
    err_s = (err_b or b"").decode("utf-8", "replace")
    return {"ok": returncode == 0, "command": command, "returncode": returncode,
            "stdout": out_s[:_STDOUT_CAP_BYTES],
            "stderr": err_s[:_STDOUT_CAP_BYTES],
            "truncated": (len(out_s) > _STDOUT_CAP_BYTES
                          or len(err_s) > _STDOUT_CAP_BYTES)}


def _drain_bounded(proc) -> tuple[bytes, bytes]:
    """The main process exited — but a daemon grandchild can still hold the
    stdout pipe, blocking communicate() forever. Bound it; on hang, kill the
    group and drain, keeping what we captured."""
    try:
        return proc.communicate(timeout=_COMMUNICATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _kill_group_and_reap(proc)


def _run_cancellable(command: str, timeout: int, sid, chat_cancel) -> dict[str, Any]:
    """New process group so the chat Stop button can kill the whole tree
    mid-build (team-mode Doer runs here on tmux-less hosts). The pgid is
    registered from the PARENT right after spawn."""
    import time as _t
    proc = subprocess.Popen(
        command, shell=True, cwd=root(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True)
    try:
        chat_cancel.track_pgid(sid, os.getpgid(proc.pid))
    except Exception:  # noqa: BLE001
        pass
    deadline = _t.monotonic() + timeout
    while proc.poll() is None:
        if chat_cancel.is_cancelled(sid):
            _kill_group_and_reap(proc)
            return _err_result(command, "stopped by user", stopped=True)
        if _t.monotonic() > deadline:
            _kill_group_and_reap(proc)
            return _err_result(command, "timeout", truncated=True)
        _t.sleep(0.2)
    out_b, err_b = _drain_bounded(proc)
    # The return shape matches the non-cancellable path — callers/tests rely on
    # ``truncated``.
    return _completed_result(command, proc.returncode, out_b, err_b)


def _run_plain(command: str, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, shell=True, cwd=root(),
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": "timeout", "command": command,
                "stdout": (exc.stdout or b"").decode("utf-8", "replace")[
                    :_STDOUT_CAP_BYTES],
                "stderr": (exc.stderr or b"").decode("utf-8", "replace")[
                    :_STDOUT_CAP_BYTES],
                "truncated": True}
    return _completed_result(command, proc.returncode, proc.stdout, proc.stderr)


def _fallback_run(command: str, timeout: int) -> dict[str, Any]:
    from aiforge_core.runtime import chat_cancel
    sid = chat_cancel.active()
    if sid is None:
        return _run_plain(command, timeout)
    if chat_cancel.is_cancelled(sid):
        return _err_result(command, "stopped by user", stopped=True)
    try:
        return _run_cancellable(command, timeout, sid, chat_cancel)
    except Exception as exc:  # noqa: BLE001
        return _err_result(command, str(exc))


def bash(
    command: str,
    *,
    restart: bool = False,
    timeout: int = _DEFAULT_TIMEOUT_S,
    _run_id: str | None = None,
) -> dict[str, Any]:
    """Run ``command`` in the persistent session for ``_run_id``.

    Falls back to stateless subprocess when tmux is unavailable. Soft-
    error contract: failures return ``{ok: False, error, ...}``.
    """
    if not command or not command.strip():
        return _err_result(command or "", "empty_command")
    # Delete policy: autonomous for everything else, ask before deleting.
    from aiforge_core.runtime.tools import delete_guard
    if not delete_guard.allow_delete() \
            and delete_guard.is_destructive_delete(command):
        return _err_result(command, delete_guard.REFUSAL, blocked="delete")
    # Optional Docker sandbox (sub #7) takes precedence when opted in.
    from aiforge_core.runtime import docker_sandbox
    if docker_sandbox.is_enabled():
        return docker_sandbox.exec_in_container(
            _effective_run_id(_run_id), command, timeout=timeout,
        )

    if not _tmux_available():
        emit("BashFallback", {"reason": "tmux_missing"})
        return _fallback_run(command, timeout)

    _run_id = _effective_run_id(_run_id)

    name = _session_name(_run_id)
    if restart and _session_exists(name):
        destroy_session(_run_id)
    _create_session(_run_id)

    if command.rstrip().endswith("&"):
        subprocess.run(
            ["tmux", "send-keys", "-t", name, command, "Enter"],
            check=True, capture_output=True,
        )
        return {
            "ok": True, "command": command, "backgrounded": True,
            "returncode": 0, "stdout": "", "truncated": False,
        }

    subprocess.run(
        ["tmux", "send-keys", "-t", name, command, "Enter"],
        check=True, capture_output=True,
    )
    body, rc, timed_out = _drain_until_prompt(name, timeout)
    if timed_out:
        subprocess.run(
            ["tmux", "send-keys", "-t", name, "C-c"],
            capture_output=True,
        )
        time.sleep(0.5)
        partial, _rc2, _t2 = _drain_until_prompt(name, 2)
        partial = _strip_echoed_command(partial, command)
        return _err_result(command, "timeout",
                           stdout=partial[:_STDOUT_CAP_BYTES], truncated=True)
    body = _strip_echoed_command(body, command)
    return {
        "ok": (rc == 0),
        "returncode": rc,
        "command": command,
        "stdout": body[:_STDOUT_CAP_BYTES],
        "truncated": len(body) > _STDOUT_CAP_BYTES,
    }


def destroy_session(run_id: str) -> None:
    """Kill the tmux session associated with ``run_id`` (best-effort)."""
    name = _active_sessions.pop(run_id, _session_name(run_id))
    if not _tmux_available():
        return
    if _session_exists(name):
        subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True,
        )
        emit("BashSession", {"session": name, "action": "destroyed"})
