"""Sandbox wrapper for ``code_run`` / ``bash`` shell invocations.

KISS: pure command-builder. Caller (handler.do_code_run /
do_bash) prepends ``wrap_cmd(args)`` before exec when
``AIFORGE_DOER_SANDBOX`` env is set.

Two modes:
- ``firejail`` — Linux process jail, no daemon. Default on Linux when
  ``firejail`` binary is on PATH.
- ``docker`` — fall-back, slower, requires running daemon. Used when
  ``AIFORGE_DOER_SANDBOX=docker``.
- ``off`` (default) — passthrough, no wrap.

Defaults (firejail):
- ``--quiet --noprofile --net=none`` — no network access for code_run
- ``--private-tmp --private-dev`` — fresh /tmp, no host devices
- ``--whitelist=<worktree>`` — only the worktree FS visible
- ``--rlimit-as=4096`` MB, ``--timeout=05:00:00``

Public surface:
- ``mode() -> str`` — "off" | "firejail" | "docker"
- ``wrap_cmd(cmd: list[str], *, cwd: str) -> list[str]``
- ``wrap_shell(script: str, *, cwd: str) -> str``
"""
from __future__ import annotations

import os
import shutil


def mode() -> str:
    explicit = (os.environ.get("AIFORGE_DOER_SANDBOX") or "").strip().lower()
    if explicit in ("firejail", "docker", "off"):
        return explicit
    if explicit == "1" or explicit == "true":
        return _autodetect()
    if explicit == "0" or explicit == "false":
        return "off"
    return "off"


def wrap_cmd(cmd: list[str], *, cwd: str) -> list[str]:
    """Prepend the sandbox launcher. Returns NEW list."""
    m = mode()
    if m == "firejail":
        return _firejail_prefix(cwd) + cmd
    if m == "docker":
        return _docker_prefix(cwd) + cmd
    return list(cmd)


def wrap_shell(script: str, *, cwd: str) -> str:
    """Wrap a shell-string invocation."""
    m = mode()
    if m == "off":
        return script
    if m == "firejail":
        return " ".join(_firejail_prefix(cwd)) + " bash -c " + _shquote(script)
    if m == "docker":
        return " ".join(_docker_prefix(cwd)) + " bash -c " + _shquote(script)
    return script


# ───────── helpers ─────────────────────────────────────────────────


def _autodetect() -> str:
    if shutil.which("firejail"):
        return "firejail"
    if shutil.which("docker"):
        return "docker"
    return "off"


def _firejail_prefix(cwd: str) -> list[str]:
    timeout = os.environ.get("AIFORGE_SANDBOX_TIMEOUT", "05:00:00")
    rlimit_mb = os.environ.get("AIFORGE_SANDBOX_RLIMIT_AS_MB", "4096")
    net_off = os.environ.get("AIFORGE_SANDBOX_NET", "0") != "1"
    args = [
        "firejail", "--quiet", "--noprofile",
        "--private-tmp", "--private-dev",
        f"--whitelist={cwd}",
        f"--rlimit-as={int(rlimit_mb) * 1024 * 1024}",
        f"--timeout={timeout}",
    ]
    if net_off:
        args.append("--net=none")
    return args


def _docker_prefix(cwd: str) -> list[str]:
    image = os.environ.get("AIFORGE_SANDBOX_IMAGE", "ubuntu:22.04")
    return [
        "docker", "run", "--rm",
        "--network", os.environ.get("AIFORGE_SANDBOX_NET_DOCKER", "none"),
        "--cpus", os.environ.get("AIFORGE_SANDBOX_CPUS", "2"),
        "--memory", os.environ.get("AIFORGE_SANDBOX_MEM", "4g"),
        "-v", f"{cwd}:{cwd}:rw",
        "-w", cwd,
        image,
    ]


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
