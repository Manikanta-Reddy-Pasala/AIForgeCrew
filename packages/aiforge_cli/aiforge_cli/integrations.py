"""Jira, Confluence, GitLab and email — settings only.

Using them is not this module's business: those are agent tools inside the
sandbox, so "file a Jira for this" already works in a chat and arrives here
only as a rendered tool step. What the terminal DOES need is the same thing
Settings gives the web UI — read the current config, change it, and prove the
credentials still work.

Secrets are write-only end to end: the API returns `has_token`, never the
token, and a save with an empty token keeps the stored one. This module never
prints a secret and never reads one back.
"""

from __future__ import annotations

from typing import Any

KINDS = ("jira", "confluence", "gitlab", "email")

# Fields worth showing per kind, in the order a human reads them. Anything the
# API adds later still shows up (see `extra` handling in `summary`).
FIELDS: dict[str, tuple[str, ...]] = {
    "jira": ("base_url", "user", "default_project", "has_token", "env_managed"),
    "confluence": ("base_url", "user", "default_space", "has_token", "env_managed"),
    "gitlab": ("base_url", "project", "has_token", "env_managed"),
    "email": ("host", "port", "user", "from_addr", "tls", "has_password", "env_managed"),
}

SECRET_KEYS = ("token", "password", "secret")


def is_secret(key: str) -> bool:
    return any(s in key.lower() for s in SECRET_KEYS) and not key.startswith("has_")


def summary(kind: str, data: dict[str, Any]) -> list[tuple[str, str]]:
    """(label, value) rows for one integration, secrets rendered as a fact.

    A token is shown as `configured` / `not set` — the only two states anyone
    can act on, and the only two the API is willing to reveal.
    """
    rows: list[tuple[str, str]] = []
    wanted = FIELDS.get(kind, tuple(data))
    for key in wanted:
        if key not in data:
            continue
        rows.append((key, _value(key, data[key])))
    for key in sorted(set(data) - set(wanted)):
        if is_secret(key):
            continue
        rows.append((key, _value(key, data[key])))
    return rows


def _value(key: str, value: Any) -> str:
    if key.startswith("has_"):
        return "configured" if value else "not set"
    if is_secret(key):
        return "configured" if value else "not set"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "" if value is None else str(value)


def parse_assignments(args: list[str]) -> dict[str, Any]:
    """``key=value`` pairs for `integrations set`.

    ``token=`` with nothing after it is rejected rather than sent: the API
    treats an empty token as "keep the current one", so accepting it silently
    would look like a successful wipe that wiped nothing.
    """
    patch: dict[str, Any] = {}
    for arg in args:
        key, sep, value = arg.partition("=")
        if not sep:
            raise ValueError(f"expected key=value, got '{arg}'")
        key = key.strip()
        value = value.strip()
        if is_secret(key) and not value:
            raise ValueError(f"'{key}=' is empty — omit it to keep the stored one")
        if value.lower() in ("true", "false"):
            patch[key] = value.lower() == "true"
        elif value.isdigit():
            patch[key] = int(value)
        else:
            patch[key] = value
    return patch


def link(text: str, url: str, *, enabled: bool = True) -> str:
    """An OSC 8 hyperlink — a clickable issue key in terminals that support it.

    Terminals that do not simply show the text, because the escape is ignored
    rather than printed. Off when colour is off, which is also when the output
    is being piped somewhere that would keep the escape bytes.
    """
    if not enabled or not url:
        return text
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"
