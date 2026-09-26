"""Which tools a native turn sends, read from the user's own words.

Split from ``_native`` so that module stays about the model call. The core
list is short; a family is added when the user's words point at it, when
``tool_help`` asked for it, when a tool of that family already ran, or when
an earlier turn of this session used it (``_sticky_tools``).
"""
from __future__ import annotations

import json
import re

# A user-role message the loop wrote, not the person. The body after the
# header (pytest's docs URL, a path named ticket) must not add tools either.
# A steer merged on with a blank line is the person's words and is kept.
_HARNESS_SEGMENT = re.compile(
    r"^(?:OBSERVATION:|\[loop guard|\[(?:[^\]]*not the user|system reminder)"
    r"[^\]]*\]|You (?:narrated|signalled|described) )")
# A steer, or the correction typed when a tool call is rejected. Both are
# the person's words and are merged onto the observation with a blank line.
_USER_SEGMENT = (
    "[NEW MESSAGE FROM THE USER",
    "The user rejected the last action",
)


# Blocks the server appends. They quote the tool catalog, READMEs and
# memory, so "jira" / "https://" in there would add every integration to
# the native list. The person's own words are the part before the marker.
_CUE_TAILS = (
    "\n\n---\n[Interpreted request",
    "\n\n---\n[Deliverable",
    "\n\n---\n[RESUME]",
    "\n\n---\n[Already read",
)


def _cue_body(content: str) -> str:
    text = content or ""
    for mark in _CUE_TAILS:
        text = text.split(mark)[0]
    return text


def _convo_text(convo) -> str:
    """The user's own words, including a mid-run steer.

    Tool results, loop-guard notes, and automated checks are stored as user
    messages. A URL or the word ticket inside one of those must not add web
    or Jira tools. Once a harness header starts, the rest of that message is
    its body, until a steer or a rejection correction. The system prompt
    names every integration; it is not the user asking for those tools, and
    neither is a copy of that prompt stored as a user turn."""
    parts = []
    for message in convo or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "\n\n".join(
                str(part.get("text") or "") for part in content
                if isinstance(part, dict))
        content = _cue_body(str(content))
        if content.lstrip().startswith("You are AIForge"):
            continue
        dropping = False
        for segment in content.split("\n\n"):
            stripped = segment.lstrip()
            if not stripped:
                continue
            if stripped.startswith(_USER_SEGMENT):
                dropping = False
                parts.append(segment)
                continue
            if dropping or _HARNESS_SEGMENT.match(stripped):
                dropping = True
                continue
            parts.append(segment)
    return "\n".join(parts)


def select_native_tools(convo, *, mode: str = "act", builder: str = "",
                        schemas: list | None = None,
                        session_id=None) -> list:
    """The native schemas this turn actually sends.

    The banner and the model call both use this, so the "(N tools)" line is
    the list on the wire. Family tools are added from the user's words, not
    from the system prompt (that prompt names every integration and used to
    make the count the whole gated catalog, about 63). ``tool_help`` extras
    and families this session used before stay on the list."""
    from ._tools._schemas import NATIVE_TOOL_SCHEMAS, filter_native
    if schemas is None:
        try:
            from ._catalog_gate import gate_schemas
            schemas = gate_schemas(NATIVE_TOOL_SCHEMAS)
        except Exception:  # noqa: BLE001 — never break a turn
            schemas = list(NATIVE_TOOL_SCHEMAS)
    extra = helped_names(convo) | used_families(convo)
    try:
        from . import _sticky_tools
        extra |= _sticky_tools.kept(session_id)
    except Exception:  # noqa: BLE001
        pass
    return filter_native(list(schemas), mode=mode or "act",
                         text=_convo_text(convo), builder=builder or "",
                         extra=extra)


def remember_session_tools(convo, session_id) -> None:
    """Keep the families this turn enabled for the session's later turns.
    Never raises."""
    if session_id is None:
        return
    try:
        from . import _sticky_tools
        from ._tools._families import named_families
        names = (named_families(_convo_text(convo)) | helped_names(convo)
                 | used_families(convo))
        _sticky_tools.keep(session_id, sorted(names))
    except Exception:  # noqa: BLE001
        pass


_ACTION_NAME = re.compile(r"ACTION:\s*([A-Za-z0-9_]+)")
_HELP_NAME = re.compile(
    r'ACTION:\s*tool_help\s*\nARGS_JSON:\s*(\{.*?\})', re.S)


def helped_names(convo) -> set[str]:
    """Tool or family names ``tool_help`` added earlier in this turn."""
    found: set[str] = set()
    for message in convo or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content") or ""
        if not isinstance(content, str):
            continue
        for match in _HELP_NAME.finditer(content):
            try:
                name = str(json.loads(match.group(1)).get("name") or "").strip()
            except (ValueError, TypeError):
                name = ""
            if name:
                found.add(name)
    return found


def used_families(convo) -> set[str]:
    """Families of the tools already called in this conversation."""
    from ._tools._families import family_of
    found: set[str] = set()
    for message in convo or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for match in _ACTION_NAME.finditer(content):
            fam = family_of(match.group(1))
            if fam:
                found.add(fam)
    return found
