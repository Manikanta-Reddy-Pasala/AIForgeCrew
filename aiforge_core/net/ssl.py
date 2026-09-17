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

* ``AIFORGE_CA_BUNDLE`` — path to a PEM CA bundle. Verification stays ON
  and trusts this CA, for the model endpoint, the integrations and every
  subprocess alike (see ``net.ca``); the standard ``SSL_CERT_FILE`` /
  ``REQUESTS_CA_BUNDLE`` are honoured the same way.
* ``AIFORGE_LLM_CA_BUNDLE`` — the same thing for the MODEL endpoint only,
  overriding the shared bundle when the two differ.
* ``AIFORGE_LLM_SSL_VERIFY`` — ``true`` (default) verifies normally;
  ``false`` / ``0`` / ``no`` / ``off`` disables verification, but **only
  for trusted-internal hosts** (see above). Ignored when a CA bundle is
  supplied, and ignored for public hosts.

``context_for(url)`` returns ``None`` for non-HTTPS URLs (plain
``http://`` local endpoints) so behaviour there is unchanged.
"""
from __future__ import annotations

import logging
import os
import ssl
from urllib.parse import urlsplit

from ._ssl_hosts import (  # noqa: F401  # re-exported
    _SERVICE_URL_KEYS,
    _agent_config_hosts,
    _configured_service_hosts,
    _is_intrinsically_internal_host,
    _is_trusted_internal_host,
    _mcp_endpoint_hosts,
    auto_relax_internal,
)
from ._ssl_ssrf import (  # noqa: F401  # re-exported
    SSRFBlocked,
    _ip_is_non_public,
    _resolved_addresses,
    _ssrf_allow_private,
    _validated_host,
    guard_public_url,
)

log = logging.getLogger("aiforge.tls")

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
    """The CA bundle for AIForge's own traffic.

    ``AIFORGE_LLM_CA_BUNDLE`` still wins for the model endpoint alone; anything
    else falls through to the shared resolver in ``net.ca``, so one
    ``AIFORGE_CA_BUNDLE`` covers the model, the integrations and every
    subprocess at once.
    """
    val = (os.environ.get("AIFORGE_LLM_CA_BUNDLE") or "").strip()
    if val:
        return val
    from aiforge_core.net import ca
    return ca.bundle()


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


def _port_of(url: str | None) -> int:
    """The port a URL names, or the scheme's default. A pin is fetched from the
    port we will actually talk to — an internal service on :8443 presents its
    certificate there, not on 443."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(str(url or ""))
        return int(parts.port or (443 if parts.scheme == "https" else 80))
    except (ValueError, TypeError):
        return 443


def insecure_context(url: str | None = None) -> ssl.SSLContext:
    """The context for an endpoint the operator marked as self-signed.

    It no longer disables verification, and the name is kept only because it is
    what every call site asks for. "Skip TLS verify" used to mean CERT_NONE
    with hostname checking off — scoped to one endpoint, deliberate, and still
    "anything on the path can be that host and we will never know".

    What the flag means now: TRUST THAT CERTIFICATE. The endpoint's certificate
    is pinned on first use and every later connection is verified against it
    (see ``net.trust``), so a self-signed internal Jira keeps working and a
    substituted certificate fails — which is the whole difference.

    A CONFIGURED CA BUNDLE WINS over the pin, on this path as on every other.
    An operator who pasted their root and intermediates into Settings (or set
    ``AIFORGE_CA_BUNDLE``) has said what to trust; trust-on-first-use is the
    answer for an estate that has no CA, not an override of one that does.
    Callers used to guard this themselves — ``context_for`` did, the provider
    probe did not — so ticking "skip TLS verify" silently discarded the CA the
    operator had just uploaded and pinned the leaf instead.

    Falls back to ordinary verification when nothing can be pinned (the host is
    unreachable, or trust-on-first-use is off). That fails the connection with a
    certificate error rather than opening it, which is the correct direction for
    a fallback to fail in.
    """
    bundle = _ca_bundle()
    if bundle:
        try:
            return ssl.create_default_context(cafile=bundle)
        except (OSError, ValueError):
            # ssl.SSLError is an OSError, so OSError alone covers an
            # unreadable file AND a bundle OpenSSL will not parse.
            # Loud, and then on to the pin: an unreadable bundle must not be
            # the reason an endpoint the operator marked self-signed goes dark.
            log.exception("tls: CA bundle %s is unusable", bundle)
    from aiforge_core.net import trust
    host = _host_of(url) if url else ""
    ctx = trust.context_for_pin(host, _port_of(url)) if host else None
    return ctx or _verifying_context()


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


def context_for(url: str | None) -> ssl.SSLContext | None:
    """Return the SSL context to pass to ``urlopen`` for ``url``.

    ``None`` for plain ``http://`` (and any non-https) URLs — urllib
    ignores the context there anyway, but returning ``None`` keeps the
    code path identical to the pre-existing behaviour.

    For ``https://``:
      * custom CA bundle set            → verifying context trusting it;
      * verify disabled + internal host → verifying context pinned to that
        host's own certificate (net.trust), never an unverified one;
      * everything else (incl. public)  → default verifying context.
    """
    if not url or not str(url).lower().startswith("https://"):
        return None

    ca = _ca_bundle()
    if ca:
        # Verification stays ON, anchored to the supplied CA bundle.
        return ssl.create_default_context(cafile=ca)

    if not _verify_enabled() and _is_trusted_internal_host(_host_of(url)):
        # A trusted self-hosted endpoint: verification stays ON, anchored to
        # that host's own certificate (pinned on first use). This branch used
        # to return CERT_NONE, which is the same capability with none of the
        # protection — see net/trust.py.
        return insecure_context(url)

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
        return insecure_context(url)        # verifying, pinned to that host
    ctx = context_for(url)
    if ctx is not None:
        return ctx
    return _ca_bundle() or True
