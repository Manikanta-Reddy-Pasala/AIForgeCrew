"""Replay the loop's text history as native tool-calling messages.

Every ``assistant.tool_calls`` must be answered by a ``role: tool`` message
with its id before the next non-tool message; a strict OpenAI-compatible
server returns 400 otherwise. The loop does not always write an OBSERVATION
after an ACTION: a rejected call, a steer that arrived first, and a loop
guard note all follow the ACTION directly. Those calls get a short "not run"
result. A steer or a rejection correction merged onto an OBSERVATION is the
person's words, so it goes out as a user message, not inside the tool result.
"""
from __future__ import annotations

import json
import re

_ACTION_BLOCK = re.compile(
    r"ACTION:\s*([A-Za-z0-9_]+)\s*\nARGS_JSON:\s*(\{.*\})", re.S)

# The person's words merged onto an observation with a blank line.
_USER_TAIL = re.compile(
    r"\n\n(?=\[NEW MESSAGE FROM THE USER|The user rejected the last action"
    r"|The user REJECTED the )")


def _parse_action_block(content: str):
    match = _ACTION_BLOCK.search(content or "")
    if not match:
        return None
    try:
        args = json.loads(match.group(2))
    except (ValueError, TypeError):
        return None
    if not isinstance(args, dict):
        return None
    return match.group(1), args


def _not_run_reason(nxt) -> str:
    """Why an ACTION has no OBSERVATION, from the message that followed it."""
    body = (nxt or {}).get("content") if isinstance(nxt, dict) else ""
    body = body if isinstance(body, str) else ""
    head = body.lstrip()
    if "REJECTED" in head[:80] or head.startswith("The user rejected"):
        return "not run: the user rejected this call"
    if head.startswith("[NEW MESSAGE FROM THE USER"):
        return "not run: a new message from the user arrived first"
    if head.startswith("[loop guard"):
        return "not run: stopped by the loop guard"
    return "not run: no result was recorded"


def _split_observation(body: str) -> tuple[str, str]:
    """``(tool result, user words merged after it)``."""
    text = body[len("OBSERVATION:"):]
    match = _USER_TAIL.search(text)
    if not match:
        return text.strip(), ""
    return text[:match.start()].strip(), text[match.end():].strip()


def to_native_messages(convo) -> list[dict]:
    """Replay ACTION steps as ``assistant.tool_calls`` and the following
    OBSERVATION as a ``role: tool`` result. Every call gets a result. Other
    messages pass through."""
    out: list[dict] = []
    index = 0
    messages = list(convo or [])
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            index += 1
            continue
        content = message.get("content") if isinstance(message.get("content"), str) else ""
        parsed = _parse_action_block(content) if message.get("role") == "assistant" else None
        if parsed is None:
            out.append(message)
            index += 1
            continue
        name, args = parsed
        call_id = f"call_{index}"
        out.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }],
        })
        nxt = messages[index + 1] if index + 1 < len(messages) else None
        nxt_body = (nxt or {}).get("content") if isinstance(nxt, dict) else ""
        tool = {"role": "tool", "tool_call_id": call_id, "name": name}
        if (isinstance(nxt, dict) and nxt.get("role") == "user"
                and isinstance(nxt_body, str) and nxt_body.startswith("OBSERVATION:")):
            result, words = _split_observation(nxt_body)
            out.append({**tool, "content": result})
            if words:
                out.append({"role": "user", "content": words})
            index += 2
            continue
        out.append({**tool, "content": _not_run_reason(nxt)})
        index += 1
    return out


def flatten_tool_messages(messages: list[dict]) -> list[dict]:
    """The same history with no tool roles: each call and its result become
    plain text, and neighbouring messages of one role are joined. For a
    server that rejected the native replay; the tools stay on the request."""
    out: list[dict] = []
    for message in messages or []:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls = "; ".join(
                f"{(c.get('function') or {}).get('name')}"
                f"({(c.get('function') or {}).get('arguments') or '{}'})"
                for c in message["tool_calls"])
            item = {"role": "assistant", "content": f"Called {calls}"}
        elif role == "tool":
            item = {"role": "user", "content":
                    f"Result of {message.get('name') or 'the call'}:\n"
                    f"{message.get('content') or ''}"}
        else:
            item = dict(message)
        prev = out[-1] if out else None
        if (prev is not None and prev.get("role") == item.get("role")
                and item.get("role") in ("user", "assistant")
                and isinstance(prev.get("content"), str)
                and isinstance(item.get("content"), str)):
            prev["content"] = prev["content"] + "\n\n" + item["content"]
            continue
        out.append(item)
    return out
