"""An answer that says a Jira issue / Confluence page was created must be backed
by a tool call that succeeded in this turn.

The model sometimes writes "I created the Confluence page" (or "pushed it") from
the chat history, from a file it wrote locally, or after a call that failed. The
user then looks for the page and it is not there. This is the claim guard for
external systems, like the file-edit claim guard beside it.
"""
from __future__ import annotations

import re

#: Tools that change an external system, and the system each one is.
_WRITE_TOOLS = {
    "jira_create": "jira", "jira_update": "jira", "jira_comment": "jira",
    "jira_transition": "jira", "jira_assign": "jira",
    "confluence_create": "confluence", "confluence_update": "confluence",
    "confluence_comment": "confluence", "confluence_add_label": "confluence",
}
_VERB = (r"(?:created|creating|added|filed|raised|opened|pushed|published|posted|"
         r"updated|saved|uploaded|committed)")
_CLAIM = re.compile(
    rf"\b(?:i(?:'ve| have)?|we(?:'ve| have)?|it(?:'s| is| has been)?|has been|have been|was|is now)\s+"
    rf"(?:\w+\s+){{0,3}}{_VERB}\b", re.I)
_SYSTEMS = (("confluence", re.compile(r"\bconfluence\b|\bwiki\b|\bpage\b", re.I)),
            ("jira", re.compile(r"\bjira\b|\bticket\b|\bissue\b|\b[A-Z][A-Z0-9]+-\d+\b")))
_NEGATED = re.compile(r"\b(?:not|n't|never|unable|could ?n[o']t|failed|no)\b[^.]{0,40}$", re.I)


def note_external(st, name: str, result) -> None:
    """Remember which external systems a tool really changed this turn."""
    system = _WRITE_TOOLS.get(str(name or "").lower())
    if not system or not isinstance(result, dict) or result.get("ok") is not True:
        return
    done = getattr(st, "external_ok", None)
    if done is None:
        done = st.external_ok = set()
    done.add(system)


def unbacked_claims(text: str, done: "set | None") -> "list[str]":
    """The systems the answer claims to have changed with no successful tool call
    for them. Conservative: a claim needs a past-tense verb about it AND the
    system named in the same sentence."""
    done = done or set()
    missing: list = []
    for sentence in re.split(r"(?<=[.!?\n])\s+", text or ""):
        if not _CLAIM.search(sentence):
            continue
        m = _CLAIM.search(sentence)
        # "I have NOT created…", "I could not create…": the denial sits before
        # the verb, inside the match or just before it.
        if _NEGATED.search(sentence[:m.end() - 1]):
            continue
        for system, rx in _SYSTEMS:
            if rx.search(sentence) and system not in done and system not in missing:
                missing.append(system)
    return missing


def nudge_text(systems: "list[str]") -> str:
    names = " and ".join("Confluence" if s == "confluence" else "Jira" for s in systems)
    return ("[loop guard — not the user] Your answer says something was created, "
            f"pushed or updated in {names}, but no {names} tool call succeeded "
            "in this turn. Call the tool now (jira_create / confluence_create "
            "/ confluence_update) and report what IT returns (system, id, url, "
            "status), or say plainly that nothing was created.")


def disclaimer(systems: "list[str]") -> str:
    names = " and ".join("Confluence" if s == "confluence" else "Jira" for s in systems)
    return (f"(Nothing was actually created or changed in {names} in this turn — "
            "no tool call for it succeeded. Ask me to do it and I will, and "
            "report what the system returns.)\n\n")
