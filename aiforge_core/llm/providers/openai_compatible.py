"""Generic OpenAI-compatible provider — the deploy-anywhere endpoint.

Reads ``base_url`` + optional ``api_key`` + ``model`` from the per-role
``agent_config`` (set on the home page), with env vars overriding. One
provider covers LM Studio, OpenRouter, Groq, Together, vLLM, and any
cloud OpenAI-compat endpoint. Blank key = no token (OSS endpoints).

Resolution (highest first):
- base_url:  ``AIFORGE_<ROLE>_BASE_URL`` → ``AIFORGE_OPENAI_COMPAT_BASE_URL``
             → agent_config row base_url → ``http://127.0.0.1:1234/v1``
- api_key:   ``AIFORGE_OPENAI_COMPAT_API_KEY`` → ``AIFORGE_<ROLE>_API_KEY``
             → agent_config row api_key → ``"not-needed"``
- model:     ``AIFORGE_<ROLE>_MODEL`` → agent_config row model → ``"default"``
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from ..types import Endpoint
from . import register_provider

log = logging.getLogger("aiforge.provider.openai_compatible")

_DEFAULT_BASE = "http://127.0.0.1:1234/v1"
_NO_TOKEN = "not-needed"


def _user_agent() -> str:
    """User-Agent for outbound LLM HTTP — one definition, shared.

    This is the provider's model-listing/probe traffic; the completions
    themselves go through llm.client and the ADK builder. All three used to
    carry their own copy of a curl-like string, so changing the agent meant
    finding three places (and the probes would have kept lying while the
    completions told the truth). ``AIFORGE_LLM_USER_AGENT`` still overrides,
    for a proxy/WAF that insists on something specific — the reason the
    curl-like default existed at all.
    """
    from aiforge_core.llm.user_agent import user_agent
    return user_agent()


def _ensure_v1(url: str) -> str:
    """Normalise an OpenAI-compatible base URL.

    Append ``/v1`` only when the URL carries no real path — a bare host
    like ``http://box:1234`` becomes ``…/1234/v1``. When the operator
    already supplied a path (``…/v1`` for vLLM/LM Studio, or ``…/api``
    for Open WebUI whose OpenAI surface lives under ``/api``), respect it
    verbatim instead of force-appending ``/v1`` and 404-ing.
    """
    from urllib.parse import urlsplit
    url = url.rstrip("/")
    path = urlsplit(url if "://" in url else "//" + url, scheme="http").path
    if path and path not in ("", "/"):
        return url  # operator-supplied path (/v1, /api, …) wins
    return url + "/v1"


def _config_row(role: str) -> dict:
    try:
        from aiforge_core.config import agent_config as _acfg
        return _acfg.get(role) or {}
    except Exception:
        return {}


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def is_available(self) -> bool:
        # Always available; connection errors propagate to the caller.
        return True

    def rate_limits(self) -> dict | None:
        return None

    def endpoint(self, role: str) -> Endpoint:
        role_up = role.upper()
        row = _config_row(role)
        base_url = (
            os.environ.get(f"AIFORGE_{role_up}_BASE_URL")
            or os.environ.get("AIFORGE_OPENAI_COMPAT_BASE_URL")
            or row.get("base_url")
            or _DEFAULT_BASE
        )
        base_url = _ensure_v1(base_url)
        api_key = (
            os.environ.get("AIFORGE_OPENAI_COMPAT_API_KEY")
            or os.environ.get(f"AIFORGE_{role_up}_API_KEY")
            or row.get("api_key")
            or _NO_TOKEN
        )
        model = (
            os.environ.get(f"AIFORGE_{role_up}_MODEL")
            or row.get("model")
            or "default"
        )
        return Endpoint(
            base_url=base_url, api_key=api_key, model=model,
            provider=self.name, role=role,
            # Carry the per-role TLS opt-out so the client._post path can
            # skip verification for this endpoint (mirrors the LiteLLM path).
            extras={"insecure_tls": bool(row.get("insecure_tls"))},
        )


def _probe_timeout(explicit: float | None) -> float:
    if explicit is not None:
        return explicit
    try:
        return float(os.environ.get("AIFORGE_LLM_PROBE_TIMEOUT_S", "15"))
    except ValueError:
        return 15.0


def probe(base_url: str, api_key: str | None = None,
          timeout: float | None = None, insecure: bool = False) -> dict:
    """Test-connection helper for the home page. GETs ``{base}/models``
    and returns ``{ok, models: [ids], error?}``. Never raises.

    ``insecure=True`` skips TLS verification for THIS probe only — the
    operator explicitly ticked "skip TLS verify" for a self-signed /
    internal HTTPS endpoint they're deliberately testing. It never
    relaxes any other host.
    """
    if not base_url or not base_url.strip():
        return {"ok": False, "error": "base_url required", "models": []}
    url = _ensure_v1(base_url.strip()) + "/models"
    is_https = url.lower().startswith("https://")
    headers = {"Accept": "application/json", "User-Agent": _user_agent()}
    has_token = bool(api_key and api_key.strip() and api_key.strip() != _NO_TOKEN)
    if has_token:
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    from .._ssl import auto_relax_internal as _ssl_auto_relax
    from .._ssl import context_for as _ssl_context_for
    from .._ssl import insecure_context as _ssl_insecure
    # Skip TLS verify when explicitly asked OR for a trusted-internal host
    # (self-hosted LAN box, self-signed cert is normal there). Public hosts
    # always verify. See net.ssl.auto_relax_internal.
    auto = (not insecure) and _ssl_auto_relax(url)
    skip_tls = is_https and (insecure or auto)
    # Diagnostic: shows the EXACT url hit, whether TLS verification was
    # skipped (and why), and token presence (never the token itself). Grep
    # the API logs for "probe ->" to confirm the running build's behaviour.
    tls_mode = (("skip-verify(auto-internal)" if auto else "skip-verify(CERT_NONE)")
                if skip_tls else "verify" if is_https else "plain-http")
    log.info("probe -> url=%s insecure_flag=%s tls=%s token=%s",
             url, insecure, tls_mode, "yes" if has_token else "no")
    try:
        # Inside the try so a bad CA bundle path (FileNotFoundError) is
        # reported as a clean {ok: False, error} instead of raising.
        if skip_tls:
            ctx = _ssl_insecure()
        else:
            ctx = _ssl_context_for(url)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(
                req, timeout=_probe_timeout(timeout), context=ctx) as r:
            payload = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        log.warning("probe FAILED url=%s tls=%s: %s", url, tls_mode, exc)
        return {"ok": False, "error": str(exc), "models": []}
    log.info("probe OK url=%s tls=%s", url, tls_mode)
    data = payload.get("data") if isinstance(payload, dict) else None
    models = []
    if isinstance(data, list):
        models = [m.get("id") for m in data
                  if isinstance(m, dict) and m.get("id")]
    return {"ok": True, "models": models}


register_provider(OpenAICompatibleProvider())
