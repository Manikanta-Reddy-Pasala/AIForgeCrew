"""Tool families a native turn can add to its short core list.

The native call sends about twenty core tools. An integration (Jira, web,
GitLab, …) is added when the user's words point at it, when an earlier turn
of the same session used it (see ``_sticky_tools``), or when the model asks
for it with ``tool_help`` and the family name. The system rules carry a
one-line index of these families so the model knows they exist.
"""
from __future__ import annotations

import re

# An issue key such as ONE-356. Case-sensitive, and common non-issue tokens
# of the same shape (UTF-8, SHA-256, ISO-8601) are ignored.
_ISSUE_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-\d+\b")
_NOT_ISSUE = frozenset({
    "UTF", "SHA", "ISO", "RFC", "CVE", "PEP", "GPT", "HTTP", "TLS", "SSL",
    "MD", "AES", "RSA", "COVID", "ES", "ECMA", "IEEE", "X", "H", "MP", "P",
    "CP", "WIN", "IE",
})

FAMILY_CUE = (
    ("jira", re.compile(
        r"\bjira\b|\btickets?\b|\bjql\b|\bsprints?\b|\bbacklog\b|\bepics?\b",
        re.I)),
    ("confluence", re.compile(
        r"\bconfluence\b|\bwiki\b|\b(?:doc(?:umentation)?|design|runbook|"
        r"team|space|wiki|confluence)\s+pages?\b|\bpage\s+(?:titled|called|"
        r"named)\b", re.I)),
    ("gitlab", re.compile(
        r"\bgitlab\b|(?-i:\bMRs?\b)|\bmerge[ -]requests?\b|\bpipelines?\b|"
        r"(?-i:\bCI\b)|\bci/cd\b", re.I)),
    ("email", re.compile(
        r"\be-?mails?\b|\bmail\b|\bsmtp\b|\binbox\b", re.I)),
    ("web", re.compile(
        r"https?://|\bweb\b|\binternet\b|\bgoogle\b|\blook\s+(?:it\s+)?up\b|"
        r"\bonline\b|\bwebsite\b|\bweb_fetch\b|\bweb_crawl\b", re.I)),
    ("watch", re.compile(
        r"\bwatch_until\b|\bwatch until\b|\bpoll until\b|\bwait until\b",
        re.I)),
    ("schedule", re.compile(r"\bschedule_task\b|\bschedule\b|\bcron\b", re.I)),
    ("services", re.compile(
        r"\bserve\b|\bdev server\b|\bnpm run dev\b|\bstart the (?:app|server)\b",
        re.I)),
    ("ui", re.compile(r"\bui_check\b|\bscreenshot\b", re.I)),
    ("codegraph", re.compile(
        r"\bcodegraph\b|\bcallers\b|\bcallees\b|\bcall graph\b|\bwho calls\b|"
        r"\bblast radius\b", re.I)),
)

FAMILY_TOOLS = {
    "watch": ("watch_until",),
    "schedule": ("schedule_task",),
    "services": ("serve", "stop_service", "list_services"),
    "ui": ("ui_check", "ui_ask"),
    "codegraph": (
        "codegraph_query", "codegraph_callers", "codegraph_callees",
        "codegraph_impact", "codegraph_explore",
    ),
}
FAMILY_PREFIX = {
    "jira": "jira_", "confluence": "confluence_", "gitlab": "gitlab_",
    "email": "email_", "web": "web_",
}
SHARED_WHEN = {
    "jira": ("context_gather", "set_integration_default"),
    "confluence": ("context_gather", "set_integration_default"),
}
# One line each in the system rules. No tool names here: the rules must
# only name tools that are on the request.
FAMILY_BLURB = {
    "jira": "Jira issues: search, read, comment, move status, log work",
    "confluence": "Confluence pages: search, read, write",
    "gitlab": "GitLab merge requests, issues and CI pipelines",
    "email": "read and send mail",
    "web": "fetch or crawl a web page by URL",
    "watch": "wait for a condition (a port, a file, a job)",
    "schedule": "run something later or on a timer",
    "services": "start, list and stop a background app",
    "ui": "screenshot a running page and check it",
    "codegraph": "symbol callers, callees and impact",
}
FAMILIES = tuple(name for name, _ in FAMILY_CUE)


def named_families(text: str) -> set[str]:
    """Families the text points at."""
    text = text or ""
    found = {name for name, cre in FAMILY_CUE if cre.search(text)}
    if any(m.group(1) not in _NOT_ISSUE for m in _ISSUE_KEY.finditer(text)):
        found.add("jira")
    return found


def family_of(tool: str) -> str:
    """The family a tool belongs to, or ""."""
    for fam, names in FAMILY_TOOLS.items():
        if tool in names:
            return fam
    for fam, prefix in FAMILY_PREFIX.items():
        if tool.startswith(prefix):
            return fam
    return ""


def family_members(fam: str, names, *, shared_too: bool = True) -> set[str]:
    """The names in ``names`` that family ``fam`` adds. A tool several
    families share does not by itself make a family available."""
    prefix = FAMILY_PREFIX.get(fam, "")
    own = set(FAMILY_TOOLS.get(fam, ()))
    shared = set(SHARED_WHEN.get(fam, ())) if shared_too else set()
    return {n for n in names
            if n in own or n in shared or (prefix and n.startswith(prefix))}


def family_index(names, core, allowed=None) -> str:
    """One line per family that would add a tool this turn, or ""."""
    names = set(names)
    lines = []
    for fam in FAMILIES:
        members = family_members(fam, names, shared_too=False) - set(core)
        if allowed is not None:
            members &= set(allowed)
        if members:
            lines.append(f"- {fam}: {FAMILY_BLURB[fam]}")
    if not lines:
        return ""
    return ("\n\nMore tools are available in families. To add one, call "
            "tool_help with the family name, for example "
            '{"name": "jira"}. Its tools are on your next step.\n'
            + "\n".join(lines))
