"""OpenHands-parity microagents (sub #5).

Markdown files under ``~/.aiforge/microagents/`` (override via
``AIFORGE_MICROAGENTS_DIR``) with YAML frontmatter declaring keyword
``triggers``. When a trigger substring appears in the prompt or last
tool output, the body is rendered as a delimited block and prepended
to the next agent prompt.

This is a deterministic, file-based recall — orthogonal to AiForgeMemory
which is similarity-ranked. Microagents are best for short canonical
playbooks ("when you see pytest, remember to add conftest.py first").
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_DIR = Path.home() / ".aiforge" / "microagents"
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Microagent:
    name: str
    type: str               # knowledge | repo | task
    triggers: tuple[str, ...]
    priority: int
    body: str
    source: str = ""


def _dir() -> Path:
    raw = os.environ.get("AIFORGE_MICROAGENTS_DIR")
    return Path(raw).expanduser() if raw else _DEFAULT_DIR


def _parse_file(path: Path) -> Microagent | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    name = str(meta.get("name") or path.stem)
    mtype = str(meta.get("type") or "knowledge")
    triggers_raw = meta.get("triggers") or []
    if not isinstance(triggers_raw, list):
        return None
    triggers = tuple(str(t).lower() for t in triggers_raw if isinstance(t, str))
    try:
        priority = int(meta.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0
    body = m.group(2).strip()
    # ``type: repo`` agents are always loaded (no triggers needed); they
    # describe project-wide conventions and live in repo-level state.
    if not name or not body:
        return None
    if mtype != "repo" and not triggers:
        return None
    return Microagent(
        name=name, type=mtype, triggers=triggers,
        priority=priority, body=body, source=str(path),
    )


def load_microagents(directory: Path | None = None) -> list[Microagent]:
    """Read every ``*.md`` under ``directory`` (default
    ``~/.aiforge/microagents``) and return the parsed list."""
    d = directory if directory is not None else _dir()
    if not d.exists():
        return []
    out: list[Microagent] = []
    for path in sorted(d.glob("*.md")):
        ma = _parse_file(path)
        if ma is not None:
            out.append(ma)
    return out


def match(text: str, agents: list[Microagent]) -> list[Microagent]:
    """Return microagents that apply to ``text``, sorted by priority desc.

    ``type: repo`` agents are ALWAYS included (project-wide context).
    Other types match by trigger-substring (case-insensitive).
    """
    low = (text or "").lower()
    hits: list[Microagent] = []
    for ma in agents:
        if ma.type == "repo":
            hits.append(ma)
            continue
        if low and any(t in low for t in ma.triggers):
            hits.append(ma)
    return sorted(hits, key=lambda m: -m.priority)


def render_injection(matches: list[Microagent]) -> str:
    """Render matched microagents as a delimited block for prompt injection."""
    if not matches:
        return ""
    parts: list[str] = []
    for ma in matches:
        parts.append(
            f"<microagent name=\"{ma.name}\" type=\"{ma.type}\">"
            f"\n{ma.body}\n</microagent>"
        )
    return "\n\n".join(parts)


__all__ = [
    "Microagent",
    "load_microagents",
    "match",
    "render_injection",
]
