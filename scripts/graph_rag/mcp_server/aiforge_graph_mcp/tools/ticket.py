"""Ticket tools: fetch + full brief composer."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Support running the mcp server without installing the scripts package:
# add graph_rag dir to sys.path so ticket_client / ticket_brief import cleanly.
_GRAPH_RAG = Path(__file__).resolve().parents[3]
if str(_GRAPH_RAG) not in sys.path:
    sys.path.insert(0, str(_GRAPH_RAG))

import ticket_client  # type: ignore
import ticket_brief as brief_mod  # type: ignore

from ..cypher_lib import NEO4J_URI


def ticket_fetch(args: dict) -> dict:
    return ticket_client.fetch(args["id"], args.get("provider", "default"))


def ticket_brief(args: dict) -> dict:
    tid = args.get("id")
    text = args.get("text")
    provider = args.get("provider", "default")
    return brief_mod.build(tid, text, provider, NEO4J_URI)


TOOLS = [
    {
        "name": "ticket_fetch",
        "description": "Fetch a ticket by id from configured provider. Returns title/body/status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "provider": {"type": "string", "default": "default"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "ticket_brief",
        "description": (
            "Compose an all-in-one ticket brief: candidate services, symbols, impact, "
            "build/test/deploy info, kube status, related memories. If `id` is omitted "
            "pass `text` to brief against ad-hoc problem text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "text": {"type": "string"},
                "provider": {"type": "string", "default": "default"},
            },
        },
    },
]

HANDLERS = {
    "ticket_fetch": ticket_fetch,
    "ticket_brief": ticket_brief,
}
