"""Turning a node into the text the rules read, and measuring its substance.

One place, so the three rule modules cannot disagree about what "the node" is.
A rule reading only the body while another reads the title is exactly how a
credential in a title travels.
"""
from __future__ import annotations

import re

# Markdown furniture that says nothing about whether a node is knowledge.
_FURNITURE = re.compile(r"^\s*(?:[-*+]\s+|#+\s+|>\s+|\d+\.\s+)", re.MULTILINE)
_FENCE = re.compile(r"```.*?```", re.DOTALL)


def text_of(node: dict) -> str:
    """Everything a rule may read: the title and the body, as one string."""
    meta = node.get("meta") or {}
    title = str(meta.get("title") or meta.get("id") or meta.get("key") or "")
    return f"{title}\n{node.get('body') or ''}"


def title_of(node: dict) -> str:
    meta = node.get("meta") or {}
    return str(meta.get("title") or "")


def substance(node: dict) -> str:
    """The body with markdown furniture removed — what is left to judge."""
    return _FURNITURE.sub("", str(node.get("body") or "")).strip()


def code_fences(node: dict) -> list[str]:
    return _FENCE.findall(str(node.get("body") or ""))


__all__ = ["text_of", "title_of", "substance", "code_fences"]
