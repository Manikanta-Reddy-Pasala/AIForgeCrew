"""Fastpath regex matchers for explicit symbols, tickets, and file paths.

When the user query contains an unambiguous identifier, skip the LLM
translator and go straight to Cypher.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Class.method or package.Class.method (last segment is the method)
_SYMBOL_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]+)(?:\.([a-z_][A-Za-z0-9_]*))+\b")
# Ticket id: ABC-123
_TICKET_RE = re.compile(r"\b([A-Z]{2,5}-\d+)\b")
# File path with extension we care about
_FILE_RE = re.compile(
    r"(?:^|\s)([A-Za-z0-9_./\-]+\.(?:py|java|ts|tsx|js|kt|go|rs))(?=\s|$)"
)


@dataclass
class FastpathHit:
    kind: str          # "symbol" | "ticket" | "file"
    value: str         # raw match


def detect(text: str) -> FastpathHit | None:
    if (m := _TICKET_RE.search(text)):
        return FastpathHit(kind="ticket", value=m.group(1))
    if (m := _FILE_RE.search(" " + text + " ")):
        return FastpathHit(kind="file", value=m.group(1))
    if (m := _SYMBOL_RE.search(text)):
        return FastpathHit(kind="symbol", value=m.group(0))
    return None
