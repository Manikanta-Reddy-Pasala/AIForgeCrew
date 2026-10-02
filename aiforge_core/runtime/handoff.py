"""A stuck run restarts from a handoff, not from its own failed transcript.

A run that is going in circles carries the circle in its prompt: every failed
attempt is there, and the model repeats the pattern. What other agents do about
it (Reflexion, OpenHands' structured summary, Cline's new-task handoff) is the
same: write down what is DONE and VERIFIED, what FAILED and why, the current
error and the next step, and continue from a fresh context that holds only that
and the goal. This module builds that record without a model call, from facts
the loop already keeps: the task board, the files written, the failed attempts
and the last error.
"""
from __future__ import annotations

import re

MAX_FAILED = 10
_ERR = re.compile(r"(?:Traceback|Error\b|Exception\b|FAILED|\berror:|AssertionError)", re.I)
_ACTION = re.compile(r"ACTION:\s*([A-Za-z_]\w*)")
_ARGS = re.compile(r"ARGS_JSON:\s*(\{.*\})", re.S)
_HEAD = 140


def note_failed(items: list, text: str) -> None:
    """Remember one approach that did not work (newest last, deduplicated, bounded)."""
    text = " ".join(str(text or "").split())[:240]
    if not text:
        return
    if text in items:
        items.remove(text)
    items.append(text)
    del items[:-MAX_FAILED]


def render_failed(items: list) -> str:
    return "\n".join(f"- {t}" for t in items[-MAX_FAILED:])


def last_attempt(convo: list) -> str:
    """``tool(args) -> first error line`` for the most recent tool call, or ''."""
    result = ""
    for m in reversed(convo or []):
        content = str((m or {}).get("content") or "")
        role = (m or {}).get("role")
        if role == "user" and content.startswith("OBSERVATION:") and not result:
            hits = [ln.strip() for ln in content.splitlines()[:80]
                    if _ERR.search(ln) and len(ln.strip()) > 8
                    and not ln.strip().startswith("Traceback")]
            # the line that names the error is last in a traceback
            line = hits[-1] if hits else ""
            result = (line or content[len("OBSERVATION:"):].strip().splitlines()[0]
                      if content[len("OBSERVATION:"):].strip() else "")[:_HEAD]
        elif role == "assistant":
            mt = _ACTION.search(content)
            if not mt:
                continue
            ma = _ARGS.search(content)
            args = (ma.group(1) if ma else "")[:90]
            return f"{mt.group(1)}({args}) -> {result or 'no output'}"
    return ""


def _title(item: dict) -> str:
    return str(item.get("title") or item.get("goal") or "").strip()


def build_chat(st) -> dict:
    """The handoff fields for a chat run, from its state."""
    from aiforge_core.runtime.chat_agent._turn._tasks import board_items
    board = board_items(getattr(st, "board", {}) or {})
    done = [_title(i) for i in board if i.get("status") == "done"]
    open_ = [_title(i) for i in board if i.get("status") not in ("done", "failed", "skipped")]
    files = list((getattr(st, "file_hashes", {}) or {}).keys())
    err = last_attempt(getattr(st, "convo", []))
    return {
        "goal": str(getattr(st, "goal", "") or "").strip()[:1200],
        "done": done[-12:],
        "open": open_[:12],
        "files": [f for f in files][-20:],
        "failed": list(getattr(st, "failed_approaches", []) or [])[-MAX_FAILED:],
        "error": err,
    }


MARK = "[HANDOFF"
RESUME_HEAD = ("[HANDOFF — the previous turn in this chat ended before the work "
               "was finished. This holds what is known; keep what is done, do "
               "not repeat what failed, and carry on from NEXT.]")
_DEFAULT_HEAD = ("[HANDOFF — the earlier attempt went in circles, so this is a "
                 "fresh start. It holds what is known; do not repeat what failed.]")


def render(h: dict, offload_id: "str | None" = None,
           header: "str | None" = None, resumed: bool = False) -> str:
    """The handoff as the one user message a restarted run starts from.

    ``header`` replaces the first line (a condense restart is not a stuck one).
    ``resumed``: it seeds the NEXT turn of a chat (after a Stop, a crash or a
    give-up) rather than a restart inside a run that went in circles."""
    lines = [header or (RESUME_HEAD if resumed else _DEFAULT_HEAD)]
    if h.get("goal"):
        lines.append(f"GOAL: {h['goal']}")
    if h.get("done"):
        lines.append("DONE (verified): " + "; ".join(h["done"]))
    if h.get("files"):
        lines.append("FILES CHANGED SO FAR: " + ", ".join(h["files"]))
    if h.get("failed"):
        lines.append("ALREADY TRIED AND FAILED — choose something different:\n"
                     + render_failed(h["failed"]))
    if h.get("green"):
        lines.append("TESTS: the last full test run was green.")
    if h.get("error"):
        lines.append("LAST ERROR / RESULT: " + h["error"])
    if h.get("steers"):
        lines.append("THE USER REDIRECTED THE WORK (these override the goal "
                     "above where they differ):\n"
                     + "\n".join(f"- {t}" for t in h["steers"][-3:]))
    if h.get("open"):
        lines.append("NEXT: " + h["open"][0])
    else:
        lines.append("NEXT: work out the smallest step that moves the goal "
                     "forward, do it, and check it.")
    offload_id = offload_id or h.get("offload")
    if offload_id:
        lines.append(f'The full earlier transcript is saved: memory_lookup '
                     f'{{"id": "{offload_id}"}} (only if you need a detail).')
    return "\n".join(lines)


__all__ = ["MARK", "RESUME_HEAD", "note_failed", "render_failed", "last_attempt", "build_chat", "render",
           "MAX_FAILED"]
