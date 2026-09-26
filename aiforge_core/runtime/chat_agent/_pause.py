"""Reads made before an ASK, or during a plan, survive the next message.

The saved history keeps the question and the answer, not the file contents.
The next turn gets those observations back once, and only when it carries on
the same task: the approved plan being executed, or a planning turn that
continues after its question. An unrelated follow-up starts clean. A result
cut to fit is marked partial, and the model may read it again.
"""
from __future__ import annotations

_MAX_OBS = 12
_MAX_CHARS = 2000
_PARTIAL = (f"\n[partial: cut at {_MAX_CHARS} characters. Read it again if "
            "you need the rest.]")
_HEADER = ("\n\n---\n[Already read for this task. Reuse these results "
           "instead of repeating the same lookup. A result marked partial was "
           "cut; read it again when you need more of it.]\n")
_pauses: dict[int, dict] = {}


def save(session_id, convo, *, asked: bool = False) -> None:
    """Keep the latest observations for this session. Never raises."""
    if session_id is None:
        return
    obs: list[str] = []
    for message in convo or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content") or ""
        if isinstance(content, str) and content.startswith("OBSERVATION:"):
            if len(content) > _MAX_CHARS:
                content = content[:_MAX_CHARS] + _PARTIAL
            obs.append(content)
    if not obs and not asked:
        return
    _pauses[int(session_id)] = {"obs": obs[-_MAX_OBS:], "asked": bool(asked)}


def take(session_id) -> dict | None:
    if session_id is None:
        return None
    return _pauses.pop(int(session_id), None)


def inject(convo: list[dict], pause: dict | None, *, plan_mode: bool = False,
           plan_exec: bool = False) -> bool:
    """Merge saved reads into the latest user turn when this turn continues
    the task they were read for: the approved plan being executed
    (``plan_exec``), or planning that goes on after its one question.
    Returns whether a plan question was already asked."""
    if not pause or not convo:
        return False
    asked = bool(pause.get("asked"))
    if not (plan_exec or (plan_mode and asked)):
        return False
    obs = pause.get("obs") or []
    if obs:
        block = _HEADER + "\n".join(obs)
        for message in reversed(convo):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = content + block
            break
    return asked and plan_mode


def reset() -> None:
    _pauses.clear()
