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

import re
import shutil
import subprocess
import time
import uuid
from typing import Any

from aiforge_core.runtime.sandbox import root

from ._trace import emit

_STDOUT_CAP_BYTES = 8000
_DEFAULT_TIMEOUT_S = 90
_POLL_INTERVAL_S = 0.1
_PROMPT_PS1 = r"PS1='__AIFORGE_PROMPT_$?__\n'"
_SENTINEL_RE = re.compile(r"__AIFORGE_PROMPT_(\d+)__")

_active_sessions: dict[str, str] = {}


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
    while time.monotonic() < deadline:
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


def _fallback_run(command: str, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=root(),
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": "timeout",
            "command": command,
            "stdout": (exc.stdout or b"").decode("utf-8", "replace")[
                :_STDOUT_CAP_BYTES
            ],
            "stderr": (exc.stderr or b"").decode("utf-8", "replace")[
                :_STDOUT_CAP_BYTES
            ],
            "truncated": True,
        }
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": command,
        "stdout": out[:_STDOUT_CAP_BYTES],
        "stderr": err[:_STDOUT_CAP_BYTES],
        "truncated": (
            len(out) > _STDOUT_CAP_BYTES or len(err) > _STDOUT_CAP_BYTES
        ),
    }


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
        return {"ok": False, "error": "empty_command"}
    # Delete policy: autonomous for everything else, ask before deleting.
    from aiforge_core.runtime.tools import delete_guard
    if not delete_guard.allow_delete() \
            and delete_guard.is_destructive_delete(command):
        return {"ok": False, "blocked": "delete", "error": delete_guard.REFUSAL}
    # Optional Docker sandbox (sub #7) takes precedence when opted in.
    from aiforge_core.runtime import docker_sandbox
    if docker_sandbox.is_enabled():
        if _run_id is None:
            _run_id = "default-" + uuid.uuid4().hex[:8]
        return docker_sandbox.exec_in_container(
            _run_id, command, timeout=timeout,
        )

    if not _tmux_available():
        emit("BashFallback", {"reason": "tmux_missing"})
        return _fallback_run(command, timeout)

    if _run_id is None:
        _run_id = "default-" + uuid.uuid4().hex[:8]

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
        return {
            "ok": False, "error": "timeout", "command": command,
            "stdout": partial[:_STDOUT_CAP_BYTES], "truncated": True,
        }
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
