"""Reads made before an ASK, or during a plan, survive the next message.

The saved history keeps the question and the answer, not the file contents.
The next turn of the same session gets those observations back once.
"""
from __future__ import annotations

_MAX_OBS = 12
_MAX_CHARS = 2000
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
            obs.append(content[:_MAX_CHARS])
    if not obs and not asked:
        return
    _pauses[int(session_id)] = {"obs": obs[-_MAX_OBS:], "asked": bool(asked)}


def take(session_id) -> dict | None:
    if session_id is None:
        return None
    return _pauses.pop(int(session_id), None)


def inject(convo: list[dict], pause: dict | None) -> bool:
    """Merge saved reads into the latest user turn. Returns whether a plan
    question was already asked."""
    if not pause or not convo:
        return False
    obs = pause.get("obs") or []
    if obs:
        block = ("\n\n---\n[Already read — do not repeat these lookups]\n"
                 + "\n".join(obs))
        for message in reversed(convo):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = content + block
            break
    return bool(pause.get("asked"))


def reset() -> None:
    _pauses.clear()
