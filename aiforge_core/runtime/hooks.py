"""Lifecycle hooks — Claude Code parity, LOCAL-only, soft-fail.

Register shell commands that fire on chat-agent lifecycle events for
deterministic automation and guardrails: run a formatter after every edit,
block a tool before it runs, log to a file. NO network, NO cloud — hooks are
plain local subprocesses.

Config is JSON, read from BOTH:

  1. ``$AIFORGE_CONFIG_DIR/hooks.json``  (global, default ``~/.aiforge``)
  2. ``<cwd>/.aiforge/hooks.json``       (repo-local — merges over global)

matching each event's hook lists are concatenated (global first, then repo),
so both sets fire — the same way skills/rules layer repo over global.

Schema (Claude Code-inspired, simplified)::

    {
      "PreToolUse":  [{"matcher": "run_command|file_write",
                       "command": "...", "block_on_nonzero": false}],
      "PostToolUse": [{"matcher": "*", "command": "..."}],
      "Stop":        [{"command": "..."}]
    }

``matcher`` is a tool-name pattern: ``*`` (or omitted) = all, ``a|b|c`` =
alternation, anything else = exact match. Each matching hook's ``command`` runs
as a LOCAL shell subprocess with the payload piped in as JSON on stdin and
exposed via env vars (``AIFORGE_HOOK_EVENT`` / ``AIFORGE_HOOK_TOOL`` /
``AIFORGE_HOOK_CWD``). Timeout is ``AIFORGE_HOOK_TIMEOUT_S`` (default 30s).

Everything soft-fails: a missing / malformed ``hooks.json``, a hook that
errors or times out → logged, NEVER raised, NEVER blocks — unless a
**PreToolUse** hook sets ``block_on_nonzero: true`` and exits non-zero, in
which case ``fire()`` returns ``blocked: True`` and the caller skips the tool.

Master kill switch: ``AIFORGE_HOOKS_DISABLE=1`` makes ``fire()`` a no-op.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

from aiforge_core.config import _filecache

log = logging.getLogger(__name__)

_EVENTS = ("PreToolUse", "PostToolUse", "Stop")
_NOOP = {"ok": True, "blocked": False, "results": []}


def _global_path() -> str:
    root = os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge"))
    return os.path.join(root, "hooks.json")


def _repo_path(cwd: str | None) -> str | None:
    if not cwd:
        return None
    return os.path.join(cwd, ".aiforge", "hooks.json")


def _load_event_hooks(event: str, cwd: str | None) -> list[dict]:
    """Concatenated hook list for *event* — global first, then repo-local.

    ``_filecache.read_json`` is mtime-cached and returns ``None`` on a
    missing / unparseable file, so a malformed hooks.json degrades to 'no
    hooks' instead of raising.
    """
    out: list[dict] = []
    for path in (_global_path(), _repo_path(cwd)):
        if not path:
            continue
        data = _filecache.read_json(path)
        if not isinstance(data, dict):
            continue
        rows = data.get(event)
        if isinstance(rows, list):
            out.extend(h for h in rows if isinstance(h, dict))
    return out


def _matches(matcher, tool: str | None) -> bool:
    if not matcher or matcher == "*":
        return True
    if tool is None:
        return False
    return tool in str(matcher).split("|")


def _timeout_s() -> float:
    try:
        return float(os.environ.get("AIFORGE_HOOK_TIMEOUT_S", "30"))
    except (TypeError, ValueError):
        return 30.0


def _run_one(hook: dict, event: str, tool: str | None, payload: dict,
             cwd: str | None) -> dict:
    """Run one hook command locally; return ``{command, returncode, ...}``.

    Any failure (spawn error, timeout) is captured into the result dict, never
    raised — the caller must not break on a bad hook.
    """
    cmd = hook.get("command")
    if not cmd or not isinstance(cmd, str):
        return {"command": cmd, "ok": False, "error": "no command"}
    env = dict(os.environ)
    env["AIFORGE_HOOK_EVENT"] = event
    env["AIFORGE_HOOK_TOOL"] = tool or ""
    env["AIFORGE_HOOK_CWD"] = cwd or ""
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd or None, env=env,
            input=json.dumps(payload, default=str),
            capture_output=True, text=True, timeout=_timeout_s())
        return {"command": cmd, "returncode": proc.returncode,
                "ok": proc.returncode == 0}
    except subprocess.TimeoutExpired:
        log.warning("hook timed out (%s): %s", event, cmd)
        return {"command": cmd, "ok": False, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001 — a bad hook must never crash a turn
        log.warning("hook failed (%s): %s: %s", event, cmd, exc)
        return {"command": cmd, "ok": False, "error": str(exc)}


def fire(event: str, payload: dict | None = None,
         cwd: str | None = None) -> dict:
    """Run every hook matching *event* and return the outcome.

    Returns ``{"ok": bool, "blocked": bool, "results": [...]}``. ``blocked`` is
    only ever ``True`` for a **PreToolUse** hook that declared
    ``block_on_nonzero`` and exited non-zero — the caller then skips the tool.
    A no-op (disabled / no matching hooks / any internal error) returns
    ``{"ok": True, "blocked": False, "results": []}``.
    """
    if os.environ.get("AIFORGE_HOOKS_DISABLE") == "1":
        return dict(_NOOP)
    if event not in _EVENTS:
        return dict(_NOOP)
    payload = payload or {}
    tool = payload.get("tool")
    try:
        matching = [h for h in _load_event_hooks(event, cwd)
                    if _matches(h.get("matcher"), tool)]
        if not matching:
            return dict(_NOOP)
        results: list[dict] = []
        blocked = False
        for hook in matching:
            res = _run_one(hook, event, tool, payload, cwd)
            results.append(res)
            if (event == "PreToolUse" and hook.get("block_on_nonzero")
                    and res.get("returncode") not in (0, None)):
                blocked = True
        return {"ok": True, "blocked": blocked, "results": results}
    except Exception as exc:  # noqa: BLE001 — hooks must NEVER break the turn
        log.warning("hooks.fire soft-fail (%s): %s", event, exc)
        return dict(_NOOP)


# ── ADK pipeline adapters ────────────────────────────────────────────────
# Fire the same PreToolUse/PostToolUse hooks at the pipeline Doer's tool-call
# boundary (the chat path fires them in chat_agent's dispatch loop). fire()
# spawns subprocesses, so run it in an executor to avoid stalling ADK's async
# loop; PreToolUse is awaited (its result can veto the tool), PostToolUse is
# fire-and-forget.

def adk_before_tool_callback():
    """ADK ``before_tool_callback`` that fires PreToolUse hooks. Returns a
    soft block-result dict when a ``block_on_nonzero`` hook vetoes the tool,
    else ``None`` (allow). No-op when hooks are disabled."""
    if os.environ.get("AIFORGE_HOOKS_DISABLE") == "1":
        return None

    async def _cb(*, tool=None, args=None, tool_context=None, **_kw):
        try:
            import asyncio
            name = getattr(tool, "name", "") or ""
            cwd = os.environ.get("AIFORGE_REPO_ROOT") or None
            payload = {"tool": name, "args": args or {}}
            out = await asyncio.get_running_loop().run_in_executor(
                None, fire, "PreToolUse", payload, cwd)
            if out.get("blocked"):
                return {"ok": False, "blocked": "hook",
                        "error": f"'{name}' was blocked by a PreToolUse hook"}
        except Exception as exc:  # noqa: BLE001 — hooks never break the run
            log.debug("adk PreToolUse hook soft-fail: %s", exc)
        return None
    return _cb


def adk_after_tool_callback():
    """ADK ``after_tool_callback`` that fires PostToolUse hooks (best-effort,
    never blocks). No-op when hooks are disabled."""
    if os.environ.get("AIFORGE_HOOKS_DISABLE") == "1":
        return None

    async def _cb(*, tool=None, args=None, tool_context=None,
                  tool_response=None, **_kw):
        try:
            import asyncio
            name = getattr(tool, "name", "") or ""
            cwd = os.environ.get("AIFORGE_REPO_ROOT") or None
            payload = {"tool": name, "args": args or {}, "result": tool_response}
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, fire, "PostToolUse", payload, cwd)
        except Exception as exc:  # noqa: BLE001
            log.debug("adk PostToolUse hook soft-fail: %s", exc)
        return None
    return _cb
