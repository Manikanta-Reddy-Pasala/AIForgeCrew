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
