"""Advertise only the integrations this install can actually use.

The chat system prompt is ~29,000 characters (~7,300 tokens) and lists 102
tools — 20 of them Jira, 11 Confluence, 7 GitLab — on EVERY turn, before any
memory, repo map or history is added. On a local model that preamble is a
drift source in itself: the more near-synonymous tool names in front of it,
the likelier it answers "get my tickets" with the issue *creator*.

Most of that surface is dead weight on any given box. If Jira has no base URL
and token configured, twenty Jira lines teach the model nothing except that
twenty plausible-looking tools exist — and every one of them returns
``jira_not_configured`` when called.

So the catalog is filtered to what is reachable. This mirrors what the loop
already does for CodeGraph (advertised only when a real index exists for this
repo) and what Cursor does with rule globs: the tool exists in the registry
either way, it is just not *advertised* when it cannot work.

Filtering is line-based rather than block-based because the catalog interleaves
integration lines with general ones; reordering it would be a much larger and
riskier edit than dropping the lines that cannot fire.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("aiforge.chat.catalog_gate")

# Catalog line → the integration that must be reachable for it to be usable.
# ``context_gather`` and ``set_integration_default`` span Jira AND Confluence,
# so they survive while EITHER is configured (see _keep).
_PREFIX_FAMILY = (
    ("- jira_", "jira"),
    ("- confluence_", "confluence"),
    ("- gitlab_", "gitlab"),
    ("- email_", "email"),
)
_SHARED_LINES = ("- context_gather ", "- set_integration_default ")


def _jira() -> bool:
    from aiforge_core.runtime.tools.jira._core import _configured
    return bool(_configured())


def _confluence() -> bool:
    from aiforge_core.runtime.tools.confluence._config import _configured
    return bool(_configured())


def _gitlab() -> bool:
    from aiforge_core.runtime.tools.gitlab import _configured
    return bool(_configured())


def _email() -> bool:
    from aiforge_core.runtime.tools import email_tool
    conf = email_tool._conf() if hasattr(email_tool, "_conf") else {}
    return bool(conf.get("host"))


_PROBES = {"jira": _jira, "confluence": _confluence,
           "gitlab": _gitlab, "email": _email}


def configured_integrations() -> set[str]:
    """Which integrations are reachable right now. A probe that raises counts
    as configured: advertising a tool that might work beats hiding one that
    does, and this must never be the thing that breaks a turn."""
    out: set[str] = set()
    for name, probe in _PROBES.items():
        try:
            if probe():
                out.add(name)
        except Exception as exc:  # noqa: BLE001
            log.debug("integration probe %s failed: %s", name, exc)
            out.add(name)
    return out


def gate_catalog(system_prompt: str,
                 available: set[str] | None = None) -> tuple[str, list[str]]:
    """Drop catalog lines for integrations that are not configured.

    Returns ``(prompt, dropped_families)``. Disable with
    ``AIFORGE_CHAT_GATE_TOOLS=0`` — the prompt then keeps every line, exactly
    as before.
    """
    if os.environ.get("AIFORGE_CHAT_GATE_TOOLS", "1") in ("0", "false", "no"):
        return system_prompt, []
    have = configured_integrations() if available is None else set(available)
    missing = {fam for _, fam in _PREFIX_FAMILY} - have
    if not missing:
        return system_prompt, []
    keep_shared = bool({"jira", "confluence"} & have)

    def _keep(line: str) -> bool:
        stripped = line.lstrip()
        for prefix, fam in _PREFIX_FAMILY:
            if stripped.startswith(prefix):
                return fam in have
        if stripped.startswith(_SHARED_LINES):
            return keep_shared
        return True

    kept = [ln for ln in system_prompt.splitlines() if _keep(ln)]
    out = "\n".join(kept)
    # A prompt that still ENDS with instructions about tools it no longer lists
    # would be contradictory; name what's off so the model doesn't reach for it.
    out += ("\n\nNOT CONFIGURED on this install (do NOT call, do not claim you "
            "did — tell the user it needs setting up): "
            + ", ".join(sorted(missing)) + ".")
    return out, sorted(missing)


__all__ = ["configured_integrations", "gate_catalog"]
