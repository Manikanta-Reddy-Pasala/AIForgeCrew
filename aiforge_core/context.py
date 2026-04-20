"""Prompt assembler + compactor.

Hard rules (from spec §5.2):
  1. NEVER compress current task or retrieved code chunks.
  2. ONLY compress prior-hop transcripts (bulleted summary).
  3. Drop lowest-ranked memory hits if over budget.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .retrieval import Hit


@dataclass
class PriorHop:
    role: str
    summary: str


@dataclass
class PromptInputs:
    role: str
    system_prompt: str
    task_body: str
    retrieved_code: list[Hit]
    retrieved_memory: list[Hit]
    prior_hops: list[PriorHop]
    tool_schemas: list[dict]
    output_contract: str


def _section(title: str, body: str) -> str:
    return f"\n\n===== {title} =====\n{body}"


def _hit_block(h: Hit) -> str:
    cite = f"[{h.id}" + (f" {h.source}" if h.source else "") + "]"
    head = f"{cite} tier={h.tier} score={h.score:.3f}"
    if h.title:
        head += f" title={h.title!r}"
    return f"{head}\n{h.text}"


def assemble_prompt(inp: PromptInputs, budget_bytes: int) -> str:
    # 1. Hard-locked sections (never truncated)
    locked = [
        _section("SYSTEM", inp.system_prompt),
        _section("TASK", inp.task_body),
        _section("RETRIEVED CODE (do not compress)", "\n\n".join(_hit_block(h) for h in inp.retrieved_code)),
        _section("OUTPUT CONTRACT", inp.output_contract),
    ]
    if inp.tool_schemas:
        locked.append(_section("TOOLS", json.dumps(inp.tool_schemas, indent=2)))

    locked_size = sum(len(s.encode()) for s in locked)
    remaining = budget_bytes - locked_size

    # 2. Flex sections (droppable / compactible)
    flex: list[str] = []
    # memory — drop lowest-ranked first if over budget
    mem_sorted = sorted(inp.retrieved_memory, key=lambda h: h.score, reverse=True)
    mem_kept: list[Hit] = []
    mem_bytes = 0
    header_reserve = 64  # for the "MEMORY" section header
    for h in mem_sorted:
        block = _hit_block(h)
        b = len(block.encode()) + 2
        if mem_bytes + b + header_reserve > max(remaining, 0) * 0.8:
            break
        mem_kept.append(h)
        mem_bytes += b
    if mem_kept:
        flex.append(_section("RETRIEVED MEMORY", "\n\n".join(_hit_block(h) for h in mem_kept)))

    # prior hops — always a bulleted summary, never raw
    if inp.prior_hops:
        bullets = "\n".join(f"- [{p.role}] {p.summary}" for p in inp.prior_hops)
        flex.append(_section("RECENT WORK (compacted)", bullets))

    return "".join(locked + flex)


import os
import urllib.request

SUMMARIZER_URL = os.environ.get(
    "AIFORGE_SUMMARIZER_URL", "http://127.0.0.1:1234/v1/chat/completions"
)
SUMMARIZER_MODEL = os.environ.get(
    "AIFORGE_SUMMARIZER_MODEL", "qwen3-4b-thinking-2507"
)


def _llm_summarize(text: str, cap_chars: int) -> str:
    """Call the small thinking model to produce a bulleted summary <= cap_chars."""
    sys_msg = (
        "You summarize an agent's prior-hop transcript into <=5 concise bullets. "
        "Keep IDs (mem:NNN, code:path#sym). Drop reasoning chatter. "
        f"Output plain text only, total <= {cap_chars} chars."
    )
    body = {
        "model": SUMMARIZER_MODEL,
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": text[:16000]},
        ],
        "temperature": 0.1,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        SUMMARIZER_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode())
    return resp["choices"][0]["message"]["content"].strip()[:cap_chars]


def compact_hop(role: str, raw_text: str, cap_chars: int = 600) -> str:
    """Compress a prior-hop transcript to a bulleted summary under cap_chars."""
    if len(raw_text) <= cap_chars:
        return raw_text
    return _llm_summarize(raw_text, cap_chars)
