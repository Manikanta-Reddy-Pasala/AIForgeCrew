"""Cross-links BETWEEN library artifacts (skills, workflows, rules).

A rule often says "…and follow the jira-read skill first"; a workflow chains a
skill. Instead of burying that as prose, the artifact carries a ``links:`` list
in its frontmatter naming the OTHER artifacts it relates to — a lightweight
graph over the library, the same idea as the OKR notes' cross-dossier links
(``work_notes``) but for the library rather than the work-context tree.

A link is a ``kind:name`` reference (``skill:jira-read``,
``workflow:jira-ticket-to-mr``, ``rule:jira-default-project``). These are NAME
references, NOT file paths — the artifacts live in separate registries
(skills/ workflows/ rules/) resolved by name, and a repo-local copy overrides a
global one, so a path link would be brittle. A dangling link (target not
authored yet) is allowed — it marks intent, exactly like a wiki ref.

Frontmatter-only: this NEVER touches an artifact's body (a rule stays a terse
bullet, a skill stays its playbook) — only the ``links:`` metadata is unified.
"""
from __future__ import annotations

import re

_KINDS = ("skill", "workflow", "rule", "prefs")
# kind:name — name is slugged after match; allow spaces so "rule:Jira Default"
# canonicalizes to rule:jira-default rather than being rejected outright.
_REF_RE = re.compile(r"^(skill|workflow|rule|prefs):\s*([A-Za-z0-9][A-Za-z0-9._ -]*)$")
# tolerate the OKR wiki spelling [[kind/name]] on input → normalized to kind:name
_WIKI_RE = re.compile(r"^\[\[(skill|workflow|rule|prefs)/([^\]\s][^\]]*)\]\]$")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", (name or "").strip().lower()).strip("-.")


def normalize_link(raw: str) -> str | None:
    """Canonicalize ONE cross-link to ``kind:name`` (slugged name), or None if
    it isn't a recognizable artifact ref. Accepts ``kind:name`` and the legacy
    ``[[kind/name]]`` wiki spelling."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    m = _REF_RE.match(s) or _WIKI_RE.match(s)
    if not m:
        return None
    slug = _slug(m.group(2))
    return f"{m.group(1)}:{slug}" if slug else None


def normalize_links(links) -> list[str]:
    """Canonicalize + dedupe a links list (order preserved, first wins).
    Non-artifact entries are dropped so a rule's ``links:`` only ever holds
    valid ``kind:name`` refs."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in links or []:
        c = normalize_link(raw)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def parse_links(meta_value) -> list[str]:
    """Read a frontmatter ``links`` value (list, or comma string) → normalized
    refs. Tolerant of hand-edited files."""
    if isinstance(meta_value, str):
        meta_value = [p.strip() for p in meta_value.split(",")]
    if not isinstance(meta_value, list):
        return []
    return normalize_links(meta_value)


def yaml_line(links: list[str]) -> str:
    """Render the canonical ``links: [...]`` frontmatter line (JSON-encoded
    scalars so a name never corrupts the YAML). Empty list → ``links: []``."""
    import json
    if not links:
        return "links: []"
    return "links: [" + ", ".join(json.dumps(x) for x in links) + "]"


__all__ = ["normalize_link", "normalize_links", "parse_links", "yaml_line",
           "_KINDS"]
