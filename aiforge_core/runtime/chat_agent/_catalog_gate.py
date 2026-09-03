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

# Web page fetch is a lockdown switch, not a config: when it is off the two
# lines below are dead weight in exactly the way the Jira lines were. Web
# SEARCH is not in this list because it no longer exists at all — see
# runtime/tools/web_fetch.py for why it was removed.
_WEB_LINES = ("- web_fetch ", "- web_crawl ")


# Deliberately worded as a POLICY, not a missing setup: an agent told "this
# needs configuring" starts asking the user to open the network.
_WEB_OFF_NOTICE = (
    "\n\nWEB ACCESS IS OFF on this install: there is no web search, and "
    "fetching a page is disabled. Do not call web_fetch/web_crawl, and do not "
    "point `browse` at an external site — it is limited to this machine. Never "
    "claim you read something online: say what you could not verify and ask "
    "the user to PASTE THE CONTENT. Do not ask for a URL — you have no tool "
    "that could open one."
)


def _web_fetch_on() -> bool:
    try:
        from aiforge_core.net import egress as _egress
        return _egress.fetch_allowed()
    except Exception as exc:  # noqa: BLE001
        # Fails OPEN, like the integrations probe: a broken probe must not
        # break a turn. Note what that means here — we would advertise a tool
        # during a lockdown. That is acceptable only because the tools
        # themselves refuse independently (aiforge_core.net.egress); the prompt
        # is a hint, never the boundary.
        log.debug("web fetch probe failed: %s", exc)
        return True


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


def _drop_lines(system_prompt: str, have: set[str], web_on: bool) -> str:
    """The line filter itself. Split out of ``gate_catalog`` to keep that
    function under the org's cognitive-complexity threshold — the repo holds an
    "every S3776 in aiforge_core is <= 15" invariant that nothing enforces, so
    it is easy to break by adding one more branch."""
    keep_shared = bool({"jira", "confluence"} & have)

    def _keep(line: str) -> bool:
        stripped = line.lstrip()
        for prefix, fam in _PREFIX_FAMILY:
            if stripped.startswith(prefix):
                return fam in have
        if stripped.startswith(_SHARED_LINES):
            return keep_shared
        if stripped.startswith(_WEB_LINES):
            return web_on
        return True

    return "\n".join(ln for ln in system_prompt.splitlines() if _keep(ln))


def gate_catalog(system_prompt: str,
                 available: set[str] | None = None) -> tuple[str, list[str]]:
    """Drop catalog lines for integrations that are not configured, and the
    web-fetch lines when page fetching is switched off.

    Returns ``(prompt, dropped_families)`` — ``dropped_families`` covers the
    INTEGRATIONS only; the web lockdown is a policy note in the prompt, not a
    "needs setting up" family. Disable with
    ``AIFORGE_CHAT_GATE_TOOLS=0`` — the prompt then keeps every line, exactly
    as before.
    """
    if os.environ.get("AIFORGE_CHAT_GATE_TOOLS", "1") in ("0", "false", "no"):
        # This switch turns off the INTEGRATION gate. It must not also
        # re-advertise web tools on a locked-down box — the two are unrelated
        # decisions and an operator flipping one should not silently undo the
        # other.
        if _web_fetch_on():
            return system_prompt, []
        return system_prompt + _WEB_OFF_NOTICE, []
    have = configured_integrations() if available is None else set(available)
    missing = {fam for _, fam in _PREFIX_FAMILY} - have
    web_on = _web_fetch_on()
    if not missing and web_on:
        return system_prompt, []
    out = _drop_lines(system_prompt, have, web_on)
    if not web_on:
        out += _WEB_OFF_NOTICE
    if not missing:
        return out, []
    # A prompt that still ENDS with instructions about tools it no longer lists
    # would be contradictory; name what's off so the model doesn't reach for it.
    out += ("\n\nNOT CONFIGURED on this install (do NOT call, do not claim you "
            "did — tell the user it needs setting up): "
            + ", ".join(sorted(missing)) + ".")
    return out, sorted(missing)


__all__ = ["configured_integrations", "gate_catalog"]
