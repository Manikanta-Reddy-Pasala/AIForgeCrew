"""LLM transport shims.

Two backends, one signature:

    complete(role_cfg, messages, tools) -> AssistantTurn

AssistantTurn is a dict shaped like OpenAI's chat-completion assistant
message — `{"role": "assistant", "content": str|None, "tool_calls": [...]}` —
so `orchestrator.py` has a single code path.

Backends:
  - openai      → `openai` SDK pointed at LM Studio's OpenAI-compat endpoint.
  - claude_cli  → subprocess `claude --print --output-format stream-json`,
                  stream parsed and normalised. Uses Claude Code subscription.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from .config import (
    CLAUDE_BIN, CLAUDE_MODEL,
    LM_STUDIO_API_KEY, LM_STUDIO_BASE_URL,
    RoleConfig,
)


# ─────────────────────────── Types ──────────────────────────────────────
ToolCall = dict  # {"id", "type":"function", "function":{"name","arguments"(json str)}}


@dataclass
class AssistantTurn:
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: dict | None = None


# ─────────────────────────── Dispatcher ─────────────────────────────────
def complete(role_cfg: RoleConfig, messages: list[dict], tools: list[dict],
             *, timeout_s: int = 300) -> AssistantTurn:
    if role_cfg.transport == "openai":
        return _openai_complete(role_cfg.model, messages, tools, timeout_s=timeout_s)
    if role_cfg.transport == "claude_cli":
        return _claude_cli_complete(role_cfg.model, messages, tools, timeout_s=timeout_s)
    raise ValueError(f"unknown transport {role_cfg.transport!r}")


# ─────────────────────────── OpenAI path ────────────────────────────────
_OPENAI_CLIENT = None


def _get_openai_client():
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        from openai import OpenAI
        _OPENAI_CLIENT = OpenAI(base_url=LM_STUDIO_BASE_URL, api_key=LM_STUDIO_API_KEY)
    return _OPENAI_CLIENT


def _openai_complete(model: str, messages: list[dict], tools: list[dict],
                     *, timeout_s: int) -> AssistantTurn:
    client = _get_openai_client()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "timeout": timeout_s,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    resp = client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    msg = choice.message
    tool_calls_norm: list[ToolCall] | None = None
    if msg.tool_calls:
        tool_calls_norm = []
        for tc in msg.tool_calls:
            tool_calls_norm.append({
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            })
    usage = resp.usage.model_dump() if resp.usage else {}
    return AssistantTurn(
        content=msg.content,
        tool_calls=tool_calls_norm,
        finish_reason=choice.finish_reason,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        raw=resp.model_dump(),
    )


# ─────────────────────────── Claude CLI path ────────────────────────────
#
# We spawn:   claude --print --output-format stream-json --verbose \
#                    --dangerously-skip-permissions \
#                    --model <model> \
#                    --append-system-prompt-file <sys> \
#                    <user_prompt_via_stdin>
#
# Stream-json output is one JSON event per line. We extract `text` deltas
# and `tool_use` blocks, then repackage into an AssistantTurn.
#
# Tools are declared in the user prompt as a JSON sidecar; Claude Code
# does NOT natively accept OpenAI-style tool schemas via --print, so we
# ask the model to emit tool calls as fenced `<tool>{…}</tool>` blocks
# which we regex-parse. This is a known-limited path — primarily used
# for Architect, whose tool use is minimal (post_comment, set_status,
# create_child_ticket).
def _claude_cli_complete(model: str, messages: list[dict], tools: list[dict],
                         *, timeout_s: int) -> AssistantTurn:
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    user = "\n\n".join(m["content"] for m in messages if m.get("role") == "user")

    if tools:
        tool_help = "\nAvailable tools (emit calls as `<tool>{...}</tool>` JSON):\n"
        for t in tools:
            fn = t.get("function", {})
            tool_help += f"- {fn.get('name')}({', '.join(fn.get('parameters',{}).get('properties',{}).keys())})\n"
        system = (system or "") + tool_help

    sys_path: str | None = None
    if system:
        tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".md")
        tmp.write(system)
        tmp.close()
        sys_path = tmp.name

    cmd = [
        CLAUDE_BIN, "--print", "--output-format", "stream-json", "--verbose",
        "--dangerously-skip-permissions", "--model", model or CLAUDE_MODEL,
    ]
    if sys_path:
        cmd += ["--append-system-prompt-file", sys_path]

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, input=user.encode("utf-8"),
            capture_output=True, timeout=timeout_s, check=False,
        )
    finally:
        if sys_path and os.path.exists(sys_path):
            os.unlink(sys_path)

    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")

    text_parts: list[str] = []
    raw_events: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_events.append(ev)
        t = ev.get("type") or ev.get("event") or ""
        if t in ("content_block_delta",) and (ev.get("delta", {}).get("text")):
            text_parts.append(ev["delta"]["text"])
        elif t in ("message_delta", "text"):
            if ev.get("text"):
                text_parts.append(ev["text"])
        elif t == "content_block_start":
            block = ev.get("content_block", {})
            if block.get("type") == "text" and block.get("text"):
                text_parts.append(block["text"])
    text = "".join(text_parts) or None

    tool_calls = _parse_inline_tool_tags(text or "")

    usage = _extract_usage_from_events(raw_events)

    return AssistantTurn(
        content=text,
        tool_calls=tool_calls or None,
        finish_reason="stop" if proc.returncode == 0 else "error",
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        raw={"returncode": proc.returncode, "stderr": stderr[-2000:],
             "events": raw_events[-20:], "dur_s": round(time.time() - t0, 2)},
    )


# ────── `<tool>{...}</tool>` parser for Claude-cli path ──────
import re as _re
_TOOL_RE = _re.compile(r"<tool>(.*?)</tool>", _re.DOTALL)


def _parse_inline_tool_tags(text: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for i, blob in enumerate(_TOOL_RE.findall(text)):
        blob = blob.strip()
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError:
            continue
        name = payload.get("name") or payload.get("tool")
        args = payload.get("args") or payload.get("arguments") or {}
        if not name:
            continue
        calls.append({
            "id": f"claudecli_{i}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        })
    return calls


def _extract_usage_from_events(events: list[dict]) -> dict:
    # Claude stream-json emits a `message_start` / `usage` block near the
    # end. Best-effort.
    for ev in reversed(events):
        usage = ev.get("usage") or ev.get("message", {}).get("usage")
        if usage:
            return {
                "prompt_tokens": usage.get("input_tokens"),
                "completion_tokens": usage.get("output_tokens"),
            }
    return {}
