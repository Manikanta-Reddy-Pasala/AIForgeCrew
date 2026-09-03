"""TLS context resolution for AIForge's *own* HTTP traffic.

AIForge talks to a fleet of self-hosted services over plain HTTP or, when
the operator fronts them with TLS, over HTTPS with an internal or
self-signed certificate: the OpenAI-compatible model endpoint, the
embed / rerank sidecars, the memory service, the MCP servers, the
local liveness probes and AIForge's own REST API. The
stdlib ``urllib.request.urlopen`` default verifies against the system
trust store, so such an endpoint fails with
``CERTIFICATE_VERIFY_FAILED``.

``context_for(url)`` builds the ``ssl.SSLContext`` to hand to those
call sites. It is deliberately **host-scoped**: the verify opt-out only
applies to hosts AIForge controls — loopback, RFC-1918 / link-local /
unique-local private IPs, ``.local`` / ``.lan`` / ``.internal`` style
suffixes, and the explicit hosts of the configured AIForge service
base-URLs. For a genuinely public host (``api.github.com``, an arbitrary
doc URL) it returns the default *verifying* context no matter what the
env says, so the toggle can never silently strip TLS verification from
external traffic. As a second layer, the public-web call sites (``doer_tools.fetch_url``,
``docs_index._fetch``, ``memory_ingest``, the GitHub ``resolver``) do not
call this helper for their normal traffic and keep stdlib default
verification. ONE narrow exception, added deliberately: after a fetch has
already FAILED with a certificate error, ``web_tls_fallback_enabled`` allows
one unverified refetch of that page (see ``is_cert_error`` below), because a
TLS-inspecting corporate appliance otherwise makes the whole web unreadable.
It never applies to an internal host — a self-signed LAN service must stay
unreachable to a model-supplied URL rather than become readable — and the
result carries ``tls_verified: false``.

Env knobs (highest priority first):

* ``AIFORGE_LLM_CA_BUNDLE`` — path to a PEM CA bundle / cert. When set,
  verification stays ON but trusts this CA (applies to every https
  host). Also honours the standard ``SSL_CERT_FILE`` /
  ``REQUESTS_CA_BUNDLE`` if AIForge's var is unset.
* ``AIFORGE_LLM_SSL_VERIFY`` — ``true`` (default) verifies normally;
  ``false`` / ``0`` / ``no`` / ``off`` disables verification, but **only
  for trusted-internal hosts** (see above). Ignored when a CA bundle is
  supplied, and ignored for public hosts.

``context_for(url)`` returns ``None`` for non-HTTPS URLs (plain
``http://`` local endpoints) so behaviour there is unchanged.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import ssl
from urllib.parse import urlsplit

_FALSEY = {"0", "false", "no", "off", ""}
_MISSING = object()

# Hostname suffixes that denote operator-controlled internal services.
_PRIVATE_SUFFIXES = (".local", ".lan", ".internal", ".intranet", ".home", ".corp")


def _verify_enabled() -> bool:
    raw = os.environ.get("AIFORGE_LLM_SSL_VERIFY")
    if raw is None:
        return True  # secure by default
    return raw.strip().lower() not in _FALSEY


def _ca_bundle() -> str | None:
    for var in ("AIFORGE_LLM_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()
    return None


def _certifi_where() -> str | None:
    """Path to certifi's Mozilla CA bundle, or ``None`` if certifi is absent.

    Best-effort — certifi is a transitive dep (httpx/requests) but not a hard
    one, so never fail context resolution when it is missing."""
    try:
        import certifi
        return certifi.where()
    except Exception:  # noqa: BLE001
        return None


def _verifying_context() -> ssl.SSLContext:
    """A *verifying* default context that also trusts certifi's CA bundle.

    python.org macOS framework builds (and some minimal Linux images) ship an
    EMPTY system trust store, so ``ssl.create_default_context()`` there fails
    every public https with ``CERTIFICATE_VERIFY_FAILED: unable to get local
    issuer certificate``. Loading certifi's Mozilla roots on top of the OS
    store fixes that while keeping verification ON. An explicit
    ``AIFORGE_LLM_CA_BUNDLE`` / ``SSL_CERT_FILE`` still wins in ``context_for``
    (this helper is only the no-explicit-bundle path)."""
    ctx = ssl.create_default_context()
    where = _certifi_where()
    if where:
        try:
            ctx.load_verify_locations(cafile=where)
        except Exception:  # noqa: BLE001 — keep whatever the OS store gave us
            pass
    return ctx


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url if "://" in url else "//" + url, scheme="").hostname
    except ValueError:
        return None
    return host.lower() if host else None


_SERVICE_URL_KEYS = (
    "AIFORGE_LM_BASE_URL", "AIFORGE_OPENAI_COMPAT_BASE_URL",
    "AIFORGE_EMBED_URL", "AIFORGE_RERANK_URL",
    "AIFORGE_API_BASE", "AIFORGE_MEMORY_URL",
)


def _mcp_endpoint_hosts(raw: str, add) -> None:
    """Add hosts from the ``name=url,name=url`` MCP endpoints list."""
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if pair:
            add(pair.split("=", 1)[1] if "=" in pair else pair)


def _agent_config_hosts(add) -> None:
    """Add per-role base_url hosts from the agent_config catalog. Best-effort —
    never fail context resolution on a config read."""
    try:
        from aiforge_core.config import agent_config as _acfg
        for row in (_acfg.load_all() or {}).values():
            if isinstance(row, dict):
                add(row.get("base_url"))
    except Exception:  # noqa: BLE001
        pass


def _configured_service_hosts() -> set[str]:
    """Hosts of the explicitly-configured AIForge service base-URLs.

    Covers the model endpoint(s), embed/rerank sidecars, memory http, MCP
    servers and AIForge's own API. Anything an operator points a base-url env var
    at counts as a host they control, so a custom DNS name (not just a private
    IP) for the self-hosted box is trusted.
    """
    hosts: set[str] = set()

    def _add(val: str | None) -> None:
        h = _host_of(val)
        if h:
            hosts.add(h)

    env = os.environ
    for key, val in env.items():
        if key.endswith("_BASE_URL") and key.startswith("AIFORGE_"):
            _add(val)
    for key in _SERVICE_URL_KEYS:
        _add(env.get(key))
    _mcp_endpoint_hosts(env.get("AIFORGE_MCP_ENDPOINTS", ""), _add)
    _agent_config_hosts(_add)
    return hosts


def _is_intrinsically_internal_host(host: str | None) -> bool:
    """True ONLY for hosts that are internal by their NAME/IP alone — loopback,
    private-IP, link-local, ``.local``/``.lan``/… suffixes, or a bare label
    (no dot). Does NOT consult the configured base-urls, so a public SaaS host
    you merely configured is never classed internal here."""
    if not host:
        return False
    host = host.lower()
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    if host.endswith(_PRIVATE_SUFFIXES):
        return True
    # Bare-label hostnames (no dot) are LAN-internal by convention.
    if "." not in host and ":" not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return True
    except ValueError:
        pass  # not an IP literal
    return False


def _is_trusted_internal_host(host: str | None) -> bool:
    """Intrinsically-internal OR an explicitly-configured service host.

    Used by the EXPLICIT opt-out path (``context_for`` — gated on the operator
    having set ``AIFORGE_LLM_SSL_VERIFY=false``), where trusting a host the
    operator pointed a base-url at is reasonable. The default-on auto-relax
    path uses :func:`_is_intrinsically_internal_host` instead so a configured
    public SaaS endpoint is NOT silently un-verified.
    """
    if _is_intrinsically_internal_host(host):
        return True
    return bool(host) and host.lower() in _configured_service_hosts()


def auto_relax_internal(url: str | None) -> bool:
    """Should an HTTPS *model endpoint* skip TLS verification by default?

    True only for a trusted-internal host (loopback / private-IP /
    ``.local``/``.lan``/``.internal`` style / bare-label / a configured
    service host) talking HTTPS, when no CA bundle is set and the
    operator hasn't forced strict mode. Rationale: these are
    operator-controlled LAN boxes (e.g. ``https://chatai.internal``)
    where a self-signed cert is the norm, so requiring a per-endpoint
    opt-out just to reach your own model server is a footgun. PUBLIC
    hosts are never auto-relaxed — they always verify.

    Bounded to the model-endpoint call sites (probe + the LiteLLM model
    build); the shared ``context_for`` used by embed/rerank/mcp/etc. is
    unchanged. Opt out with ``AIFORGE_LLM_TLS_STRICT_INTERNAL=1`` (or set
    a CA bundle, which keeps verification on for every host).
    """
    if not url or not str(url).lower().startswith("https://"):
        return False
    if _ca_bundle():
        return False
    raw = os.environ.get("AIFORGE_LLM_TLS_STRICT_INTERNAL", "")
    if raw.strip().lower() not in _FALSEY:
        return False  # operator forced strict for internal hosts
    # Default-on path: relax ONLY intrinsically-internal hosts. A configured
    # public SaaS endpoint (openrouter.ai, api.openai.com) must keep verifying
    # unless the operator explicitly opts out (insecure_tls / SSL_VERIFY=false).
    return _is_intrinsically_internal_host(_host_of(url))


def insecure_context() -> ssl.SSLContext:
    """An explicitly non-verifying TLS context (CERT_NONE).

    For a *deliberate, per-endpoint* opt-out: the operator pasted a
    self-hosted HTTPS base-URL and ticked "skip TLS verify" in the UI
    (or stored ``insecure_tls`` on that role). Unlike the global
    ``AIFORGE_LLM_SSL_VERIFY`` toggle this is scoped to the single
    endpoint the caller is talking to, so it never strips verification
    from any other host. Callers gate it on https + the explicit flag.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ───────────────────── web fetch: the broken-chain case ─────────────────
# A corporate network that inspects TLS (a Fortinet/Zscaler-style appliance)
# re-signs every response with a CA the process does not trust, so an ordinary
# public page fails with CERTIFICATE_VERIFY_FAILED and the agent is simply
# blind to the web. The operator asked for that to stop being a wall.
#
# The rule is verify FIRST and fall back only on a certificate failure, never
# to skip verification up front: a page that can be fetched securely always is,
# and the downgrade is reported to the caller (``tls_verified: false``) rather
# than hidden. A connection refused, a 404 or a timeout is not a cert problem
# and is never retried this way.
#
# AIFORGE_WEB_INSECURE_TLS=0 forbids the fallback outright, for an operator who
# would rather see the failure. Supplying AIFORGE_LLM_CA_BUNDLE (the appliance's
# CA) is strictly better than either: verification keeps working.


def web_tls_fallback_enabled() -> bool:
    raw = os.environ.get("AIFORGE_WEB_INSECURE_TLS")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def public_verifying_context() -> "ssl.SSLContext | None":
    """A VERIFYING context for arbitrary public-web traffic, honouring the
    operator's CA bundle.

    The doer/researcher fetch used the stdlib default, which never consults
    ``AIFORGE_LLM_CA_BUNDLE`` — so installing the inspecting appliance's CA,
    the remedy this module and the fetch's own log line both recommend, fixed
    nothing there and every page still came back through the unverified
    fallback. Returns None when no bundle is configured (the stdlib default is
    then exactly right), and NEVER relaxes verification: this is the verified
    attempt.
    """
    bundle = _ca_bundle()
    if not bundle:
        return None
    try:
        return ssl.create_default_context(cafile=bundle)
    except Exception:  # noqa: BLE001 — a bad path must not break the fetch
        return None


def web_tls_fallback_allowed_for(url: str) -> bool:
    """May THIS url's certificate failure be retried without verification?

    Never for an intrinsically-internal host. The fallback exists for the
    public web behind an inspecting appliance; a self-signed LAN service is
    the opposite case — it fails closed today, and "helpfully" stripping
    verification would turn a model-supplied ``https://192.168.x.x/`` or
    ``https://vault.internal/`` from unreachable into readable. That is a
    reachability change, not a convenience.
    """
    if not web_tls_fallback_enabled():
        return False
    return not _is_intrinsically_internal_host(_host_of(url))


def is_cert_error(exc: BaseException) -> bool:
    """Is this failure specifically about certificate verification?

    Matched on the exception type where possible (``ssl.SSLCertVerificationError``
    survives being wrapped in URLError as ``.reason``) and on the message only
    as a fallback — the message is the one thing every wrapper layer preserves.
    """
    import urllib.error
    # An HTTP STATUS means the TLS handshake already SUCCEEDED, so this can
    # never be a certificate-verification failure — and HTTPError.__str__ is
    # "HTTP Error {code}: {reason}", where the reason phrase is copied verbatim
    # from the server's status line. Without this, any server (or an on-path
    # attacker who can inject a plain HTTP response but cannot forge a
    # certificate) could answer `502 certificate verify failed` and talk the
    # client into retrying with verification switched off.
    if isinstance(exc, urllib.error.HTTPError):
        return False
    seen = exc
    for _ in range(4):                      # URLError(reason=SSLError(...))
        if isinstance(seen, ssl.SSLCertVerificationError):
            return True
        nxt = getattr(seen, "reason", _MISSING)
        if nxt is _MISSING or nxt is None:
            nxt = getattr(seen, "__cause__", None)
        if nxt is None or nxt is seen:
            break
        seen = nxt
    # The message fallback applies ONLY to transport-level failures. Anything
    # else reaching here (a ValueError from a parser, an application error)
    # must not be able to spell its way into an unverified refetch.
    if not isinstance(exc, (ssl.SSLError, urllib.error.URLError, OSError)):
        return False
    text = str(exc).lower()
    return ("certificate verify failed" in text
            or "certificate_verify_failed" in text
            or "self signed certificate" in text
            or "self-signed certificate" in text
            or "unable to get local issuer" in text
            or "hostname mismatch" in text
            or "certificate has expired" in text)


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
    return raw.strip().lower() not in _FALSEY


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


def context_for(url: str | None) -> ssl.SSLContext | None:
    """Return the SSL context to pass to ``urlopen`` for ``url``.

    ``None`` for plain ``http://`` (and any non-https) URLs — urllib
    ignores the context there anyway, but returning ``None`` keeps the
    code path identical to the pre-existing behaviour.

    For ``https://``:
      * custom CA bundle set            → verifying context trusting it;
      * verify disabled + internal host → unverified context (CERT_NONE);
      * everything else (incl. public)  → default verifying context.
    """
    if not url or not str(url).lower().startswith("https://"):
        return None

    ca = _ca_bundle()
    if ca:
        # Verification stays ON, anchored to the supplied CA bundle.
        return ssl.create_default_context(cafile=ca)

    if not _verify_enabled() and _is_trusted_internal_host(_host_of(url)):
        # Scoped opt-out for a trusted self-hosted endpoint only.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # Public host, or verify left on: full default verification (with certifi
    # roots layered in so an empty OS trust store doesn't break public https).
    return _verifying_context()


def httpx_verify(url: str | None = None, *, insecure_tls: bool = False):
    """The ``verify`` value to hand an ``httpx.Client`` (and thus the OpenAI /
    instructor SDK) so those SDKs honour the EXACT SAME TLS policy litellm uses
    for the working client.complete path — otherwise a self-signed internal
    model endpoint connects on chat but 'Connection error's on the structured
    path. Mirrors ``client.py``'s resolution: an explicit ``insecure_tls`` OR an
    auto-relaxed internal host → verification OFF (unless a CA bundle pins it);
    else the per-url context / CA bundle / default verify. Returns
    True | <ssl.SSLContext>. httpx accepts both."""
    if (insecure_tls or auto_relax_internal(url)) and not _ca_bundle():
        return insecure_context()           # ssl.SSLContext with CERT_NONE
    ctx = context_for(url)
    if ctx is not None:
        return ctx
    return _ca_bundle() or True
