"""Per-session chat action/response trace — a reviewable on-disk transcript.

Every chat turn (simple AND team mode) appends a section to
``~/.aiforge/chat_traces/session_<id>.md`` — the user message, EACH action the
agent took (tool name + args + result outcome + thoughts), and the final
response — so a human can review exactly what happened per message. A
machine-readable ``session_<id>.jsonl`` sibling holds the full structured turn.

Override the location with ``AIFORGE_CHAT_TRACE_DIR``; opt out with
``AIFORGE_CHAT_TRACE=0``. Best-effort — a trace failure must never break a turn.

(The team/ticket PIPELINE also dumps a full ADK trajectory to
``~/.aiforge/trajectories/<ticket>/<run>.json`` via :mod:`trajectory`.)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def trace_dir() -> Path:
    raw = os.environ.get("AIFORGE_CHAT_TRACE_DIR")
    base = (Path(raw).expanduser() if raw
            else Path.home() / ".aiforge" / "chat_traces")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _short(val: Any, limit: int = 300) -> str:
    s = ("" if val is None else str(val)).replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _fmt_action(step: dict) -> str:
    """One markdown bullet per action, most-signal-first."""
    t = step.get("type")
    if t == "tool":
        name = step.get("name") or "?"
        res = step.get("result")
        ok = res.get("ok") if isinstance(res, dict) else None
        mark = "✅" if ok is True else ("❌" if ok is False else "•")
        return (f"- 🔧 {mark} `{name}`({_short(step.get('args'), 200)}) "
                f"→ {_short(res, 300)}")
    if t == "thought":
        return f"- 💭 {_short(step.get('text'), 300)}"
    if t == "error":
        return f"- ⚠️ ERROR: {_short(step.get('text'), 300)}"
    if t == "plan_ready":
        return "- 📋 plan ready (awaiting approval)"
    return f"- · {t}: {_short(step.get('text'), 200)}"


_ACTION_TYPES = ("tool", "thought", "error", "plan_ready")


def append_turn(*, session_id: int, prompt: str, steps: list[dict],
                final_text: str, team: bool, cwd: str = "") -> str | None:
    """Append one turn to the session trace files. Returns the md path (or
    None when disabled / on failure). Never raises."""
    if os.environ.get("AIFORGE_CHAT_TRACE", "1").strip().lower() in (
            "0", "false", "no", "off", ""):
        return None
    try:
        d = trace_dir()
        when = time.strftime("%Y-%m-%d %H:%M:%S")
        mode = "team" if team else "simple"
        actions = [s for s in (steps or [])
                   if isinstance(s, dict) and s.get("type") in _ACTION_TYPES]
        n_tools = sum(1 for s in actions if s.get("type") == "tool")

        header = f"\n## {when} · {mode}" + (f" · {cwd}" if cwd else "")
        md = [header, f"**User:** {_short(prompt, 500)}", ""]
        if actions:
            md.append(f"**Actions ({n_tools} tool call"
                      f"{'' if n_tools == 1 else 's'}):**")
            md += [_fmt_action(s) for s in actions]
            md.append("")
        md.append(f"**Response:** {_short(final_text, 2000)}")
        md.append("")
        md_path = d / f"session_{session_id}.md"
        with md_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(md) + "\n")

        rec = {"ts": when, "session_id": session_id, "mode": mode, "cwd": cwd,
               "prompt": prompt, "n_tools": n_tools,
               "actions": actions, "response": final_text}
        with (d / f"session_{session_id}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
        return str(md_path)
    except Exception:  # noqa: BLE001 — tracing must never break a turn
        return None


def read_turns(session_id: int) -> list[dict]:
    """Parsed turns for a session (from the ``.jsonl``), oldest first. Empty
    when no trace exists. Never raises."""
    try:
        p = trace_dir() / f"session_{session_id}.jsonl"
        if not p.is_file():
            return []
        out: list[dict] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001 — skip a corrupt line, keep the rest
                continue
        return out
    except Exception:  # noqa: BLE001
        return []


__all__ = ["append_turn", "read_turns", "trace_dir"]
