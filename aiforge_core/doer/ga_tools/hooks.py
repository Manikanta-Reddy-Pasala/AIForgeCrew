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
    # Edit / build / test cycle
    "post_edit", "post_compile", "post_test", "pre_commit",
    # Tool dispatch (every do_<tool> goes through here)
    "pre_tool",  "post_tool",
    # File I/O specialisations (KISS shorthand for pre_tool gates
    # that only fire on file_read / file_patch / file_write)
    "pre_file_read",  "post_file_read",
    "pre_file_write", "post_file_write",
    # Search ops (glob / grep / search_memory / unified_memory_query)
    "pre_search",  "post_search",
    # LLM round-trip (wraps every session.raw_ask call)
    "pre_llm",   "post_llm",
    # Agent lifecycle
    "agent_start", "agent_end",
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
    payload: dict | None = None,
) -> list[dict]:
    """Run every matching hook for ``event``. Returns per-hook results.

    ``payload`` (optional) is exposed to the hook as
    ``AIFORGE_HOOK_PAYLOAD`` env var (JSON-encoded). Tool / file path /
    LLM-tokens etc. live there so a custom hook can inspect what's
    happening without parsing logs.

    Result: ``{run, exit, stdout_tail, stderr_tail, blocked, wall_ms}``.
    Pure logic — caller decides what to inject into the next prompt.
    """
    import time as _t
    import json as _j
    out: list[dict] = []
    payload_env = _j.dumps(payload or {}, default=str)[:4000]
    for h in hooks:
        if h["event"] != event:
            continue
        t0 = _t.time()
        try:
            cp = subprocess.run(
                h["run"], shell=True, cwd=cwd,
                capture_output=True, timeout=h["timeout"], check=False,
                env={
                    **os.environ,
                    "AIFORGE_HOOK_EVENT":   event,
                    "AIFORGE_HOOK_PAYLOAD": payload_env,
                },
            )
            stdout = (cp.stdout or b"").decode("utf-8", "replace")[-1000:]
            stderr = (cp.stderr or b"").decode("utf-8", "replace")[-1000:]
            out.append({
                "event":  event,
                "run":    h["run"],
                "exit":   cp.returncode,
                "stdout_tail": stdout,
                "stderr_tail": stderr,
                "blocked":     bool(h["block"] and cp.returncode != 0),
                "wall_ms":     int((_t.time() - t0) * 1000),
            })
        except subprocess.TimeoutExpired:
            out.append({
                "event": event, "run": h["run"], "exit": -1,
                "stdout_tail": "", "stderr_tail": "(timeout)",
                "blocked": bool(h["block"]),
                "wall_ms": int((_t.time() - t0) * 1000),
            })
        except Exception as exc:
            out.append({
                "event": event, "run": h["run"], "exit": -2,
                "stdout_tail": "", "stderr_tail": str(exc)[:400],
                "blocked": bool(h["block"]),
                "wall_ms": int((_t.time() - t0) * 1000),
            })
    return out


# ───────── Performance recorder (always-on, KISS in-memory) ────────


_PERF: dict[str, dict] = {}


def record_step(
    *, event: str, name: str, wall_ms: int,
    extra: dict | None = None,
) -> None:
    """Record one step duration for the current run.

    KISS aggregator: no histograms, just count + total + max per
    (event, name). Reset per Doer run via :func:`reset_perf`.
    """
    key = f"{event}:{name}"
    row = _PERF.setdefault(key, {
        "event": event, "name": name,
        "count": 0, "total_ms": 0, "max_ms": 0,
    })
    row["count"] += 1
    row["total_ms"] += int(wall_ms)
    if wall_ms > row["max_ms"]:
        row["max_ms"] = int(wall_ms)
    if extra:
        row.setdefault("extra", []).append(extra)


def perf_snapshot() -> list[dict]:
    """Sorted by total wall-clock spent. Used by /api/runtime/perf."""
    rows = list(_PERF.values())
    rows.sort(key=lambda r: -r["total_ms"])
    return rows


def reset_perf() -> None:
    _PERF.clear()


# ───────── Always-on builtin: perf telemetry ───────────────────────


def emit_step(
    *, event: str, name: str, wall_ms: int,
    extra: dict | None = None,
) -> None:
    """Record + (optionally) append to ``~/.aiforge/perf.ndjson`` so
    cron / grafana can scrape it. Best-effort — never raises."""
    record_step(event=event, name=name, wall_ms=wall_ms, extra=extra)
    if os.environ.get("AIFORGE_PERF_NDJSON", "1") != "1":
        return
    try:
        import json as _j
        path = os.path.expanduser(
            os.environ.get("AIFORGE_PERF_NDJSON_PATH",
                           "~/.aiforge/perf.ndjson"),
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(_j.dumps({
                "event": event, "name": name,
                "wall_ms": wall_ms,
                "extra": extra or {},
            }) + "\n")
    except Exception:
        pass


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
