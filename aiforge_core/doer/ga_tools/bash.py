"""Bash tool — persistent shell session with sticky CWD + smart timeout.

Mirrors Claude Code's Bash. GA's ``code_run`` spawns a fresh shell
each invocation: ``cd /worktree && mvn ...`` resets after every
call, so the doer pays the ``cd`` cost on every command and can't
chain output between calls.

We keep one ``subprocess.Popen`` alive per Doer run, write commands
into its stdin, read stdout/stderr until a sentinel marker. The
shell process inherits cwd from the worktree once and stays there.
Auto-restart on crash, hard-kill on per-command timeout.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid

SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Run a shell command in the worktree. The shell session "
            "persists across calls — exports, cd, and env vars set "
            "in one call survive. Default timeout 60s, max 600s. "
            "Use for mvn / git / curl / writing scratch files. "
            "DO NOT use for grep/find/ls — call the grep / glob "
            "tools instead. Output truncated to 8 KB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command. Posix /bin/bash.",
                },
                "timeout_s": {
                    "type": "integer",
                    "default": 60,
                    "description": "Per-command timeout (max 600).",
                },
            },
            "required": ["command"],
        },
    },
}


class PersistentShell:
    """Long-lived /bin/bash subprocess with sticky CWD.

    One shell per Doer run. Write commands + a sentinel echo to
    stdin, read stdout until sentinel, return the buffer. Sentinel
    gives us framing without parsing prompts.
    """

    __slots__ = ("_proc", "_cwd", "_alive")

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd
        self._alive = False
        self._proc: subprocess.Popen | None = None

    def _spawn(self) -> None:
        self._proc = subprocess.Popen(
            ["/bin/bash", "--norc", "--noprofile"],
            cwd=self._cwd, env=os.environ.copy(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        # Make stdout non-blocking so we can poll with timeout.
        os.set_blocking(self._proc.stdout.fileno(), False)  # type: ignore[arg-type]
        self._alive = True

    def run(self, command: str, timeout_s: int) -> str:
        """Run one command, return combined stdout+stderr (trimmed).

        On timeout: SIGKILL the shell + respawn for the next call.
        Output capped at 8 KB to keep doer prompt sane.
        """
        if not self._alive or self._proc is None or self._proc.poll() is not None:
            self._spawn()
        sentinel = f"__AIFORGE_DONE_{uuid.uuid4().hex[:8]}__"
        # Wrap the command so the sentinel always prints (success or
        # fail) and any stderr is funnelled into stdout.
        wrapped = (
            f"{{ {command}; }} 2>&1; "
            f"printf '\\n%s_RC=%d\\n' {shlex.quote(sentinel)} \"$?\"\n"
        )
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(wrapped)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._spawn()
            return f"[bash] shell respawned after pipe break\n"
        deadline = time.time() + max(1, min(timeout_s, 600))
        buf: list[str] = []
        rc_marker = f"{sentinel}_RC="
        while time.time() < deadline:
            if self._proc.poll() is not None:
                buf.append("[bash] shell died\n")
                self._alive = False
                break
            try:
                chunk = self._proc.stdout.read(8192)  # type: ignore[union-attr]
            except BlockingIOError:
                chunk = ""
            if chunk:
                buf.append(chunk)
                if rc_marker in "".join(buf):
                    break
            else:
                time.sleep(0.05)
        else:
            # Timeout: SIGKILL + respawn for next command.
            self._proc.kill()
            self._alive = False
            return (
                f"[bash] command timed out after {timeout_s}s; "
                f"output so far:\n" + "".join(buf)[-8192:]
            )
        text = "".join(buf)
        # Strip the sentinel + RC line for cleaner output.
        idx = text.find(rc_marker)
        rc_str = ""
        if idx >= 0:
            rc_part = text[idx + len(rc_marker):].splitlines()
            rc_str = rc_part[0] if rc_part else ""
            text = text[:idx]
        text = text[-8192:]
        return f"[bash] rc={rc_str.strip()}\n{text.rstrip()}"

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        self._alive = False


def handle(shell: PersistentShell, args: dict) -> str:
    cmd = (args.get("command") or "").strip()
    if not cmd:
        return "[bash] empty command"
    timeout_s = int(args.get("timeout_s") or 60)
    return shell.run(cmd, timeout_s)
