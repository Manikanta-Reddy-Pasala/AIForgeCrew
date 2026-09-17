"""The SSRF guard: refusing URLs that resolve to non-public addresses."""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit


def _pkg():
    """``ssl``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``ssl``; patch any other
    name on this module."""
    import aiforge_core.net.ssl as package
    return package


# ─────────────────────────── SSRF guard ─────────────────────────────────
# Shared guard for the public-fetch paths (the researcher's
# ``web_read`` and the ``kind=url`` memory ingest) plus the Doer browser
# allowlist. Parses a URL, requires an http(s) scheme, resolves the host via
# DNS and REJECTS if ANY resolved address is private / loopback / link-local
# (169.254.0.0/16 cloud IMDS) / reserved / multicast / unspecified. Without
# this, a model-supplied URL can pivot to ``http://169.254.169.254/`` (cloud
# metadata / credentials), ``http://127.0.0.1:<port>/`` internal services, or
# an RFC-1918 LAN host. Escape hatch: ``AIFORGE_SSRF_ALLOW_PRIVATE=1`` for an
# operator who genuinely needs to fetch an internal host (default OFF).


class SSRFBlocked(Exception):
    """Raised when a URL resolves to a non-public / disallowed address.

    ``kind`` distinguishes a definite private/metadata target (``"private"``)
    or a bad scheme (``"scheme"``) — both hard-block — from a DNS resolution
    failure (``"dns"``). Callers may choose to let ``urlopen`` surface a
    natural network error for the ``dns`` case (an unresolvable host cannot be
    an SSRF target anyway) while always refusing ``private``/``scheme``.
    """

    def __init__(self, message: str, *, kind: str = "private") -> None:
        super().__init__(message)
        self.kind = kind


def _ssrf_allow_private() -> bool:
    """Operator escape hatch — allow fetching private/internal hosts."""
    raw = os.environ.get("AIFORGE_SSRF_ALLOW_PRIVATE", "")
    return raw.strip().lower() not in _pkg()._FALSEY


def _ip_is_non_public(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validated_host(url: str) -> "tuple[str, str]":
    """(scheme, host) for a guardable url. Raises SSRFBlocked(kind="scheme") for
    an empty url, a non-http(s) scheme, or a hostless url."""
    if not url:
        raise SSRFBlocked("empty url", kind="scheme")
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SSRFBlocked(f"scheme not allowed: {scheme or '(none)'}", kind="scheme")
    if not parts.hostname:
        raise SSRFBlocked("url has no host", kind="scheme")
    return scheme, parts.hostname


def _resolved_addresses(host: str, port: int) -> list:
    """Every A/AAAA address ``host`` resolves to. Raises SSRFBlocked(kind="dns")
    when resolution fails or yields nothing usable."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise SSRFBlocked(f"dns resolution failed for {host}: {exc}",
                          kind="dns") from exc
    addrs: list[ipaddress._BaseAddress] = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            continue
    if not addrs:
        raise SSRFBlocked(f"no addresses resolved for {host}", kind="dns")
    return addrs


def guard_public_url(url: str | None) -> str:
    """Return ``url`` if it is safe to fetch, else raise :class:`SSRFBlocked`.

    Safe = an ``http(s)`` URL whose host is a public IP, or a hostname whose
    EVERY resolved address is public. Honours the
    ``AIFORGE_SSRF_ALLOW_PRIVATE=1`` escape hatch (returns ``url`` unchecked).
    A DNS resolution failure raises ``SSRFBlocked(kind="dns")`` — safer to block
    than to fetch, though callers may downgrade that to a natural ``urlopen``
    error (see class docstring).
    """
    if _ssrf_allow_private():
        return url or ""
    scheme, host = _validated_host(url)

    # IP literal → check directly (no DNS).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_non_public(literal):
            raise SSRFBlocked(f"blocked non-public address: {literal}",
                              kind="private")
        return url

    # Hostname → resolve every A/AAAA record and reject if ANY is non-public
    # (defends a name that points at an internal IP).
    port = urlsplit(url).port or (443 if scheme == "https" else 80)
    for addr in _resolved_addresses(host, port):
        if _ip_is_non_public(addr):
            raise SSRFBlocked(
                f"host {host} resolves to non-public address {addr}",
                kind="private")
    return url
