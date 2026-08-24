"""Which URLs we are willing to talk to — one place, one rule.

Plain ``http://`` over a network is credentials and prompts in cleartext, and
this process sends both (API keys in headers, whole conversations in bodies).
So HTTPS is required — for anything ROUTABLE.

LOOPBACK IS THE EXCEPTION, deliberately. A request to 127.0.0.1 never leaves
the machine, so there is no wire to sniff, and the local model stack is all
plain http: LM Studio on :1234, the embed sidecar on :8764, ollama on :11434,
the docker services run.sh brings up. Browsers draw exactly this line —
localhost is a "secure context" for the same reason. Refusing it would not make
anything safer; it would only break every local setup.

``AIFORGE_REQUIRE_HTTPS=1`` removes the exception for a deployment that fronts
its local endpoints with TLS and wants the stricter rule enforced.

This module is the ONLY place that decides. The scheme check used to be
open-coded as ``url.startswith(("http://", "https://"))`` in five call sites,
which is five chances to disagree.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0",
                             "host.docker.internal"})


def _strict() -> bool:
    return (os.environ.get("AIFORGE_REQUIRE_HTTPS", "").strip().lower()
            in ("1", "true", "yes", "on"))


def is_loopback(host: str | None) -> bool:
    """Does this host stay on the machine?"""
    if not host:
        return False
    h = host.strip().strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def check(url: str | None) -> str | None:
    """``None`` when the URL may be used, else a human-readable refusal.

    Refuses a scheme we do not speak, and plaintext http to a routable host.
    """
    raw = str(url or "").strip()
    if not raw:
        return "missing url"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return f"unparseable url: {raw[:80]!r}"
    scheme = (parts.scheme or "").lower()
    if scheme == "https":
        return None
    if scheme != "http":
        return (f"unsupported scheme {scheme or '(none)'!r} — use https://"
                f"{' or http:// for a loopback address' if not _strict() else ''}")
    if _strict():
        return ("plain http is refused (AIFORGE_REQUIRE_HTTPS=1). Use https://, "
                "or front this endpoint with TLS.")
    if is_loopback(parts.hostname):
        return None
    return (f"plain http to {parts.hostname!r} would send API keys and prompt "
            "text in cleartext over the network. Use https://, or point this "
            "at a loopback address.")


def is_allowed(url: str | None) -> bool:
    """Convenience for call sites that only need the boolean."""
    return check(url) is None


__all__ = ["check", "is_allowed", "is_loopback"]
