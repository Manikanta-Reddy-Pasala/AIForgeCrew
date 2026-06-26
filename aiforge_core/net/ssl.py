"""TLS context resolution for AIForge's *own* HTTP traffic.

AIForge talks to a fleet of self-hosted services over plain HTTP or, when
the operator fronts them with TLS, over HTTPS with an internal or
self-signed certificate: the OpenAI-compatible model endpoint, the
embed / rerank sidecars, the memory / neo4j-http service, the MCP
servers, the local liveness probes and AIForge's own REST API. The
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
external traffic. As a second layer, the public-web call sites
(``doer_tools.fetch_url``, ``docs_index._fetch``, ``memory_ingest``,
the GitHub ``resolver``) do not call this helper at all and keep stdlib
default verification.

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
import ssl
from urllib.parse import urlsplit

_FALSEY = {"0", "false", "no", "off", ""}

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


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url if "://" in url else "//" + url, scheme="").hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _configured_service_hosts() -> set[str]:
    """Hosts of the explicitly-configured AIForge service base-URLs.

    Covers the model endpoint(s), embed/rerank sidecars, memory/neo4j
    http, MCP servers and AIForge's own API. Anything an operator points
    a base-url env var at counts as a host they control, so a custom DNS
    name (not just a private IP) for the self-hosted box is trusted.
    """
    hosts: set[str] = set()

    def _add(val: str | None) -> None:
        h = _host_of(val)
        if h:
            hosts.add(h)

    env = os.environ
    # Single-valued base-url style vars (exact + AIFORGE_<ROLE>_BASE_URL).
    for key, val in env.items():
        if key.endswith("_BASE_URL") and key.startswith("AIFORGE_"):
            _add(val)
    for key in (
        "AIFORGE_LM_BASE_URL", "AIFORGE_OPENAI_COMPAT_BASE_URL",
        "AIFORGE_EMBED_URL", "AIFORGE_RERANK_URL",
        "AIFORGE_API_BASE", "AIFORGE_MEMORY_URL",
        "NEO4J_HTTP_URL", "NEO4J_URI",
    ):
        _add(env.get(key))
    # Comma/equals list of MCP endpoints: "name=url,name=url".
    for pair in (env.get("AIFORGE_MCP_ENDPOINTS", "") or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        _add(pair.split("=", 1)[1] if "=" in pair else pair)
    # Configured per-role base_urls in the agent_config catalog.
    try:  # best-effort; never fail context resolution on a config read.
        from aiforge_core.config import agent_config as _acfg
        for row in (_acfg.load_all() or {}).values():
            if isinstance(row, dict):
                _add(row.get("base_url"))
    except Exception:  # noqa: BLE001
        pass
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

    # Public host, or verify left on: full default verification.
    return ssl.create_default_context()
