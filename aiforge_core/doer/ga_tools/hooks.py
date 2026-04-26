"""Post-tool hooks — Claude-Code-style automation triggered from GA's
tool_after_callback.

Hooks are declarative, defined in ``.aiforge/hooks.yml`` at the
worktree root. KISS: only shell commands, no Python plugins. Each
hook entry:

    - event: post_edit          # or post_compile, pre_commit
      run: ./scripts/format.sh  # cwd = worktree root
      timeout: 30               # seconds, default 30
      block: false              # true = on non-zero exit, abort the
                                #        next agent turn with stderr

Loaded once per Doer run via :func:`load`; dispatched by
:func:`run_for_event` from the handler. Failures are logged but never
raise unless ``block=true`` AND the hook command exited non-zero.

Toggle via ``AIFORGE_DOER_HOOKS=1`` (default off until per-repo hook
configs are vetted).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable


_VALID_EVENTS = frozenset({
    "post_edit", "post_compile", "post_test", "pre_commit",
})


def builtin_hooks(worktree: str) -> list[dict]:
    """Always-on hooks regardless of repo config.

    Currently: secret-scan pre_commit. Disable by setting
    ``AIFORGE_DOER_SECRET_SCAN=0`` env, NOT by editing this list.
    """
    out: list[dict] = []
    if os.environ.get("AIFORGE_DOER_SECRET_SCAN", "1") != "0":
        out.append({
            "event": "pre_commit",
            "run": (
                "python -m aiforge_core.doer.ga_tools.secrets_cli "
                f"{worktree}"
            ),
            "timeout": 60,
            "block": True,
        })
    return out


def load(worktree: str) -> list[dict]:
    """Read ``.aiforge/hooks.yml`` from ``worktree`` and merge with
    builtin hooks. Returns [] only when both sources are empty."""
    out: list[dict] = list(builtin_hooks(worktree))
    cfg_path = Path(worktree) / ".aiforge" / "hooks.yml"
    if cfg_path.is_file():
        try:
            import yaml  # type: ignore
            raw = yaml.safe_load(cfg_path.read_text()) or []
        except (ImportError, Exception):
            raw = []
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                event = (entry.get("event") or "").strip().lower()
                run = (entry.get("run") or "").strip()
                if event not in _VALID_EVENTS or not run:
                    continue
                out.append({
                    "event": event,
                    "run": run,
                    "timeout": int(entry.get("timeout") or 30),
                    "block": bool(entry.get("block", False)),
                })
    return out


def run_for_event(
    hooks: Iterable[dict], event: str, *, cwd: str,
) -> list[dict]:
    """Run every matching hook for ``event``. Returns per-hook results.

    Each result: ``{run, exit, stdout_tail, stderr_tail, blocked}``.
    Pure logic — caller decides what to inject into the next prompt.
    """
    out: list[dict] = []
    for h in hooks:
        if h["event"] != event:
            continue
        try:
            cp = subprocess.run(
                h["run"], shell=True, cwd=cwd,
                capture_output=True, timeout=h["timeout"], check=False,
                env={**os.environ, "AIFORGE_HOOK_EVENT": event},
            )
            stdout = (cp.stdout or b"").decode("utf-8", "replace")[-1000:]
            stderr = (cp.stderr or b"").decode("utf-8", "replace")[-1000:]
            out.append({
                "run": h["run"],
                "exit": cp.returncode,
                "stdout_tail": stdout,
                "stderr_tail": stderr,
                "blocked": bool(h["block"] and cp.returncode != 0),
            })
        except subprocess.TimeoutExpired:
            out.append({
                "run": h["run"], "exit": -1,
                "stdout_tail": "", "stderr_tail": "(timeout)",
                "blocked": bool(h["block"]),
            })
        except Exception as exc:
            out.append({
                "run": h["run"], "exit": -2,
                "stdout_tail": "", "stderr_tail": str(exc)[:400],
                "blocked": bool(h["block"]),
            })
    return out


def render(results: list[dict]) -> str:
    """Compact summary of hook runs for prompt injection."""
    if not results:
        return ""
    lines = ["[hooks]"]
    for r in results:
        marker = "OK " if r["exit"] == 0 else f"X {r['exit']}"
        suffix = " (blocked next turn)" if r.get("blocked") else ""
        lines.append(f"  {marker} {r['run']}{suffix}")
        if r["exit"] != 0 and r["stderr_tail"]:
            lines.append(f"    stderr: {r['stderr_tail'][:300]}")
    return "\n".join(lines)


def first_blocked(results: list[dict]) -> dict | None:
    """First hook result that flagged ``block: true`` and failed."""
    for r in results:
        if r.get("blocked"):
            return r
    return None
