"""The ONE standard format for managed workspace notes.

Every markdown artifact the system writes into a shared work-context folder
(jira ticket.md/dossier.md, confluence page.md, web page.md — see
``work_context``) had its own ad-hoc shape. This module is the single
renderer/parser for all of them:

    ---                       ← YAML frontmatter (machine-readable identity)
    kind: jira
    key: PROJ-42
    source_url: https://jira/browse/PROJ-42
    updated_at: 2026-07-10T00:00:00+00:00
    links:
      - "https://jira/browse/PROJ-42"
      - "[confluence/12345](../../confluence/12345/page.md)"
    ---
    # PROJ-42 — the title

    ## Objective                ← Google-OKR sections, fixed order:
    ## Key Results                an Objective + measurable Key Results
    ## Facts                      (Google's structure); Facts/Links/
    ## Links                      Learnings extend it for dossier upkeep
    ## Learnings
    <free body — the full page/ticket text, preserved verbatim>

Cross-references to OTHER managed dossiers are stored as RELATIVE MARKDOWN
FILE LINKS to the target note's md file
(``[jira/PROJ-42](../../jira/PROJ-42/ticket.md)``) — they render/click as
plain markdown, survive a Jira/Confluence base-URL change, and the curator can
resolve them locally. Legacy ``[[kind/key]]`` wiki refs are still accepted on
input and upgraded to md links on the next write. Rendering is DETERMINISTIC
(stable section order, stable link order) so repeated writes of the same data
produce byte-identical files — the git-ignored dossier folders never look
"changed" from a re-render.

Soft-error contract: ``update_note`` returns ``{"ok": bool, ...}`` and never
raises; ``parse_note`` is tolerant of hand-edited / legacy files (missing
frontmatter, unknown sections) and never raises on str input.

This module was split (grouped by concern) into ``_helpers`` / ``_links`` /
``_render`` / ``_consolidate`` submodules; this package re-exports the full
former top-level surface (public AND private-with-leading-underscore names) so
``from aiforge_core.runtime import work_notes`` and every ``work_notes.<name>``
attribute access is unchanged.
"""
from __future__ import annotations

from ._consolidate import (
    _CONSOLIDATE_SYS,
    _JUNK_ITEM_RE,
    _ci_key,
    _consolidate_once,
    _dedupe_ci,
    _deterministic_merge,
    _okf_rules,
    _sections_dict,
    _supersede_directive,
    consolidate,
    consolidate_note,
)
from ._helpers import (
    _BODY_MARK,
    _BRIEF_REF_RE,
    _CONF_URL_RE,
    _FM_ALIASES,
    _FRONTMATTER_RE,
    _JIRA_URL_RE,
    _KEY_TO_HEADING,
    _MD_REF_RE,
    _PRIMARY_NOTE,
    _SECTION_KEYS,
    _SECTION_ORDER,
    _WIKI_REF_RE,
    _as_items,
    _log,
    _mirror_aliases,
    _now_iso,
    _yaml_str,
)
from ._links import _md_ref, normalize_links, normalize_tags
from ._render import (
    _BOILERPLATE_SUBSTR,
    _KNOWN_KINDS,
    _LEAK_ITEM_RE,
    _is_leak_item,
    knowledge_text,
    parse_note,
    render_note,
    scrub_items,
    update_note,
    validate_note,
)

__all__ = ["render_note", "parse_note", "normalize_links", "update_note",
           "knowledge_text", "consolidate", "consolidate_note"]
