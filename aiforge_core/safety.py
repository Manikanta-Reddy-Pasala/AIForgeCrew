"""Prompt-injection sanitization + network-tool auditing per DESIGN §8.

Scope:
  - `scrub_ticket_text()`: strip common prompt-injection patterns from any
    human/ticket text before forwarding it to a cloud LLM (EM's path).
  - `assert_no_network_tools()`: registry audit — fails fast if any tool
    handler can reach outside localhost (§8.2 threat control).
"""
from __future__ import annotations

import re
from typing import Iterable

# Patterns commonly seen in prompt-injection payloads pasted into tickets.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)ignore (all|prior|previous|above) (instructions?|prompts?)"),
    re.compile(r"(?i)disregard (all|your) (instructions?|rules?)"),
    re.compile(r"(?i)you are now (a|an) (new|different|unfiltered)"),
    re.compile(r"(?i)system prompt[:\s]"),
    re.compile(r"(?i)jailbreak"),
    re.compile(r"(?i)developer mode"),
    re.compile(r"<\s*/?\s*(system|admin|prompt|instructions?)\s*>"),
    re.compile(r"(?i)act as (a |an )?root"),
    re.compile(r"(?i)reveal (your )?(system|hidden) prompt"),
    # Secrets-leak lures (common injection endings)
    re.compile(r"(?i)(send|exfil|post|POST).*?(http|https|curl|wget)"),
)

_REPLACEMENT = "[REDACTED-INJECTION]"


def scrub_ticket_text(text: str, *, max_len: int = 32_000) -> str:
    """Return a sanitized copy of a user-supplied ticket body.

    - Truncates to `max_len` chars (EM cloud call cost cap).
    - Redacts patterns in `_INJECTION_PATTERNS`.
    - Removes null bytes + control chars besides newline/tab.
    """
    if not text:
        return ""
    t = text[:max_len]
    t = t.replace("\x00", "")
    # Keep \n and \t, drop other C0 controls.
    t = "".join(ch for ch in t if ch >= " " or ch in "\n\t")
    for pat in _INJECTION_PATTERNS:
        t = pat.sub(_REPLACEMENT, t)
    return t


# --------- registry audit ---------

# Substrings allowed in handler source (imports or localhost-bound calls).
_NET_DENY = (
    "urllib.request",
    "requests.get",
    "requests.post",
    "httpx",
    "aiohttp",
    "socket.",
    "urlopen",
)


def _handler_sources(tool_registry: object) -> Iterable[tuple[str, str]]:
    """Yield (tool_name, handler-closure-source-repr). Best-effort introspection."""
    import inspect

    tools = getattr(tool_registry, "_tools", None) or {}
    for name, tool in tools.items():
        handler = getattr(tool, "handler", None)
        try:
            src = inspect.getsource(handler)
        except (OSError, TypeError):
            src = repr(handler)
        yield (name, src)


#: Tools explicitly exempt from the no-network-library guard. These handlers
#: open HTTP on purpose and are capability-gated (see permissions `network_fetch`
#: and the domain allowlist at security/network-allowlist.yml).
ALLOWED_NET_TOOLS: set[str] = {"fetch_url"}


def assert_no_network_tools(tool_registry: object, *, allow: set[str] | None = None) -> None:
    """Fail fast if any non-allowed tool handler references a network library.

    The LLM client (hermes.llm) is the only permitted network caller; since it
    lives outside the tool registry it never shows up here.
    """
    allow = (allow or set()) | ALLOWED_NET_TOOLS
    violations: list[str] = []
    for name, src in _handler_sources(tool_registry):
        if name in allow:
            continue
        for bad in _NET_DENY:
            if bad in src:
                violations.append(f"{name}: references {bad!r}")
                break
    if violations:
        raise RuntimeError("network-tool audit failed:\n  - " + "\n  - ".join(violations))
