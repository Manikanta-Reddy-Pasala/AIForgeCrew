"""Which hosts count as internal: configured service and MCP hosts, private
suffixes, and relaxing verification for them."""
from __future__ import annotations

import ipaddress
import os


def _pkg():
    """``ssl``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``ssl``; patch any other
    name on this module."""
    import aiforge_core.net.ssl as package
    return package


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
        h = _pkg()._host_of(val)
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
    if host.endswith(_pkg()._PRIVATE_SUFFIXES):
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
    pkg = _pkg()
    if not url or not str(url).lower().startswith("https://"):
        return False
    if pkg._ca_bundle():
        return False
    raw = os.environ.get("AIFORGE_LLM_TLS_STRICT_INTERNAL", "")
    if raw.strip().lower() not in pkg._FALSEY:
        return False  # operator forced strict for internal hosts
    # Default-on path: relax ONLY intrinsically-internal hosts. A configured
    # public SaaS endpoint (openrouter.ai, api.openai.com) must keep verifying
    # unless the operator explicitly opts out (insecure_tls / SSL_VERIFY=false).
    return _is_intrinsically_internal_host(pkg._host_of(url))
