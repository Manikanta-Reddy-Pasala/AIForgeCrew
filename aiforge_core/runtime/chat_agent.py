"""Conversational full-filesystem coding agent (deploy-anywhere chat).

A lightweight, provider-agnostic ReAct loop — NOT the ticket pipeline.
Streams steps back to the Chat UI. The model talks a plain text
protocol (no native tool-calling) so it works across every backend the
home page can point at (LM Studio, OpenRouter, Groq, vLLM, cloud).

Tools run with TOTAL filesystem + exec freedom by design (the operator
chose whole-machine access). An optional ``AIFORGE_WORKSPACE_DIR``
clamps file/exec operations to a root for cautious deploys.

Protocol — each model turn must be either a tool call:

    THOUGHT: <reasoning>
    ACTION: <tool_name>
    ARGS_JSON: {"path": "..."}

or a final answer:

    THOUGHT: <reasoning>
    FINAL: <message to the user>

Public surface:
    run_chat_agent(messages, *, cwd, role, max_steps, complete_fn)
        -> Iterator[dict]   # SSE-ready event dicts
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterator

_ACTION_RE = re.compile(r"ACTION:\s*([A-Za-z_]+)", re.IGNORECASE)
_ARGS_RE = re.compile(r"ARGS_JSON:\s*(\{.*\})", re.IGNORECASE | re.DOTALL)
_FINAL_RE = re.compile(r"FINAL:\s*(.*)", re.IGNORECASE | re.DOTALL)
_THOUGHT_RE = re.compile(r"THOUGHT:\s*(.*?)(?:\n[A-Z_]+:|$)", re.IGNORECASE | re.DOTALL)

_MAX_OBS = 6000  # truncate tool output fed back to the model


def _workspace_root() -> Path | None:
    raw = os.environ.get("AIFORGE_WORKSPACE_DIR")
    return Path(os.path.expanduser(raw)).resolve() if raw else None


def _resolve(cwd: str, path: str) -> Path:
    """Resolve ``path`` against the session cwd. When AIFORGE_WORKSPACE_DIR
    is set, reject anything that escapes it; otherwise total freedom."""
    base = Path(cwd).expanduser().resolve()
    p = (base / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    root = _workspace_root()
    if root is not None and root not in p.parents and p != root:
        raise PermissionError(f"path escapes AIFORGE_WORKSPACE_DIR: {path}")
    return p


# ─────────────────────────── tools ──────────────────────────────────

def _t_file_read(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args["path"])
    if not p.is_file():
        return {"ok": False, "error": f"not a file: {args['path']}"}
    return {"ok": True, "content": p.read_text(encoding="utf-8", errors="replace")}


def _t_file_write(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args.get("content", ""), encoding="utf-8")
    return {"ok": True, "path": str(p), "bytes": len(args.get("content", ""))}


def _t_file_patch(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args["path"])
    if not p.is_file():
        return {"ok": False, "error": "not_found"}
    body = p.read_text(encoding="utf-8")
    old = args["old_text"]
    n = body.count(old)
    if n == 0:
        return {"ok": False, "error": "old_text_not_found"}
    if n > 1:
        return {"ok": False, "error": "ambiguous_match", "occurrences": n}
    p.write_text(body.replace(old, args["new_text"], 1), encoding="utf-8")
    return {"ok": True, "path": str(p)}


def _t_list_dir(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args.get("path", "."))
    if not p.is_dir():
        return {"ok": False, "error": f"not a dir: {args.get('path')}"}
    entries = [
        (c.name + "/") if c.is_dir() else c.name
        for c in sorted(p.iterdir())
    ]
    return {"ok": True, "entries": entries}


def _t_run_command(args: dict, cwd: str) -> dict:
    cmd = args["cmd"]
    base = cwd
    root = _workspace_root()
    if root is not None:
        base = str(root)
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=base, capture_output=True, text=True,
            timeout=int(args.get("timeout", 120)),
        )
        return {"ok": proc.returncode == 0, "code": proc.returncode,
                "stdout": proc.stdout[-_MAX_OBS:], "stderr": proc.stderr[-_MAX_OBS:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}


def _t_memory_lookup(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.memory import unified_query as _uq
        res = _uq.query(args["query"], limit=int(args.get("limit", 6)))
        return {"ok": True, "hits": [
            {"text": (h.get("text") or "")[:400], "source": h.get("source")}
            for h in res.get("hits", [])
        ]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_memory_write(args: dict, cwd: str) -> dict:
    """Persist a durable fact/decision into the knowledge memory so future
    chats + tickets recall it. repo defaults to the working dir's name."""
    try:
        from aiforge_core.runtime.tools.memory_write import memory_write as _mw
        repo = args.get("repo") or os.path.basename(os.path.normpath(cwd)) or "chat"
        return _mw(
            text=args["text"],
            kind=args.get("kind", "note"),
            tags=list(args.get("tags") or []) + ["chat"],
            decision=bool(args.get("decision")),
            repo=repo,
        )
    except KeyError:
        return {"ok": False, "error": "missing arg: text"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


TOOLS: dict[str, Callable[[dict, str], dict]] = {
    "file_read": _t_file_read,
    "file_write": _t_file_write,
    "file_create": _t_file_write,   # alias
    "file_patch": _t_file_patch,
    "list_dir": _t_list_dir,
    "run_command": _t_run_command,
    "memory_lookup": _t_memory_lookup,
    "memory_write": _t_memory_write,
}

_SYSTEM = """You are AIForge, an autonomous coding assistant with FULL access to \
the user's filesystem and shell in the working directory {cwd}.

You work by emitting ONE step at a time in this exact text format.

To use a tool:
THOUGHT: <your reasoning>
ACTION: <one of: file_read, file_write, file_create, file_patch, list_dir, run_command, memory_lookup, memory_write>
ARGS_JSON: <a single-line JSON object of the tool's arguments>

Tool arguments:
- file_read    {{"path": "rel/or/abs"}}
- file_write   {{"path": "...", "content": "..."}}      (creates/overwrites)
- file_patch   {{"path": "...", "old_text": "...", "new_text": "..."}}
- list_dir     {{"path": "."}}
- run_command  {{"cmd": "ls -la", "timeout": 120}}
- memory_lookup{{"query": "..."}}                        (recall from knowledge memory)
- memory_write {{"text": "the durable fact", "kind": "note|gotcha|decision", "decision": false}}
                (save a learning/decision to the knowledge graph for future recall)

When you are done and ready to reply to the user:
THOUGHT: <reasoning>
FINAL: <your full natural-language answer>

Rules: emit exactly one ACTION or one FINAL per turn. After each ACTION you \
receive an OBSERVATION with the tool result, then continue. Keep going until \
the task is complete, then give FINAL. Do real work — read and edit files, run \
commands — rather than guessing."""


def _parse(out: str) -> dict:
    """Parse a model turn into {kind, ...}."""
    fin = _FINAL_RE.search(out)
    act = _ACTION_RE.search(out)
    # FINAL wins only if there's no action before it (action-first agents
    # sometimes mention "final" in prose). Prefer ACTION when present.
    if act:
        name = act.group(1).strip()
        args_m = _ARGS_RE.search(out)
        args = {}
        if args_m:
            try:
                args = json.loads(args_m.group(1))
            except (ValueError, TypeError):
                args = {}
        thought = _THOUGHT_RE.search(out)
        return {"kind": "action", "tool": name, "args": args,
                "thought": thought.group(1).strip() if thought else ""}
    if fin:
        return {"kind": "final", "text": fin.group(1).strip()}
    # No protocol markers — treat the whole output as the final answer.
    return {"kind": "final", "text": out.strip()}


# Loop detection: no fixed step budget — long coding sessions run until
# the agent finishes. We stop only when it's clearly STUCK: the same
# tool+args repeated this many times, or identical model output N times
# in a row. ``_SAFETY_CAP`` is a last-resort runaway guard (very high;
# tune with AIFORGE_CHAT_SAFETY_CAP), not a normal stopping point.
_LOOP_REPEAT = 4
_OUTPUT_REPEAT = 3


def run_chat_agent(
    messages: list[dict], *,
    cwd: str,
    role: str = "doer",
    max_steps: int | None = None,   # kept for callers/tests; None = no cap
    complete_fn: Callable[..., str] | None = None,
) -> Iterator[dict]:
    """Drive the ReAct loop until the agent finishes or a stuck loop is
    detected (NOT a step count). Yields SSE-ready event dicts:

    ``{"type": "thought", "text"}`` · ``{"type": "tool", "name", "args",
    "result"}`` · ``{"type": "message", "text"}`` (final) ·
    ``{"type": "error", "text"}`` · ``{"type": "done"}``.
    """
    if complete_fn is None:
        from aiforge_core.llm.client import complete as complete_fn  # type: ignore

    import collections
    safety = max_steps or int(os.environ.get("AIFORGE_CHAT_SAFETY_CAP", "2000"))

    convo: list[dict] = [{"role": "system", "content": _SYSTEM.format(cwd=cwd)}]
    for m in messages:
        r = m.get("role") or "user"
        convo.append({"role": "assistant" if r == "assistant" else "user",
                      "content": m.get("content") or ""})

    action_counts: dict[str, int] = {}
    recent_outputs: collections.deque = collections.deque(maxlen=_OUTPUT_REPEAT)

    n = 0
    while n < safety:
        n += 1
        try:
            out = complete_fn(role, convo)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "text": f"llm error: {exc}"}
            yield {"type": "done"}
            return

        # Stuck-output loop: identical model reply N times running.
        recent_outputs.append(out.strip())
        if (len(recent_outputs) == _OUTPUT_REPEAT
                and len(set(recent_outputs)) == 1):
            yield {"type": "error",
                   "text": f"stopped: the model repeated the same response "
                           f"{_OUTPUT_REPEAT}× — breaking the loop"}
            yield {"type": "done"}
            return

        convo.append({"role": "assistant", "content": out})
        step = _parse(out)
        if step["kind"] == "final":
            yield {"type": "message", "text": step["text"]}
            yield {"type": "done"}
            return

        # action
        name = step["tool"]
        args = step["args"]
        # Stuck-action loop: same tool+args repeated too many times.
        sig = name + "|" + json.dumps(args, sort_keys=True, default=str)
        action_counts[sig] = action_counts.get(sig, 0) + 1
        if action_counts[sig] >= _LOOP_REPEAT:
            yield {"type": "error",
                   "text": f"stopped: repeated the same action '{name}' "
                           f"{action_counts[sig]}× with the same args — "
                           f"breaking the loop"}
            yield {"type": "done"}
            return

        if step.get("thought"):
            yield {"type": "thought", "text": step["thought"]}
        fn = TOOLS.get(name)
        if fn is None:
            result = {"ok": False, "error": f"unknown tool: {name}"}
        else:
            try:
                result = fn(args, cwd)
            except KeyError as exc:
                result = {"ok": False, "error": f"missing arg: {exc}"}
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
        yield {"type": "tool", "name": name, "args": args, "result": result}
        obs = json.dumps(result)[:_MAX_OBS]
        convo.append({"role": "user", "content": f"OBSERVATION: {obs}"})

    yield {"type": "message",
           "text": "(stopped: hit the runaway safety cap — "
                   "raise AIFORGE_CHAT_SAFETY_CAP if this was real work)"}
    yield {"type": "done"}
