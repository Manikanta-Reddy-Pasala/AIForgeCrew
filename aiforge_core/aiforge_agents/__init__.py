"""aiforge_agents — pluggable ADK-based agent runtime.

Per spec v0.4 (docs/SPEC-aiforge_agents-v0.4.docx). Currently lives
in this tree; will be extracted to its own repo after P0 green.

Public surface:
    from aiforge_core.aiforge_agents import registry, runtime
    agent = registry.build('planner', config=...)
    runtime.run_ticket(ticket_id)
"""
from __future__ import annotations

SCHEMA_VERSION = "agents-v0.4"
