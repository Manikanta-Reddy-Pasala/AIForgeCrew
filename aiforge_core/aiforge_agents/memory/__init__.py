"""Memory — unified client over AiForgeMemory + Postgres + files.

Per spec §7 with one decision: NO ROLE-BASED ACCESS. Every agent
gets the same access surface; agents pick what they need.

Public:
    memory.code_context(text, repo)   -> ContextBundle (AiForgeMemory)
    memory.episodic.search(query)     -> [EpisodicHit]
    memory.procedural.match(task_class) -> [Pattern]
    memory.audit.write(event)
    memory.session.append(ticket_id, text)
    memory.skills.match(intent)       -> [Skill]
    memory.prompts.get(role, version) -> str
"""
from __future__ import annotations
