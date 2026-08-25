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
_APPLICATION_JSON = "application/json"


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


def _probe_tls_plan(url: str, insecure: bool) -> "tuple[bool, str]":
    """(skip_tls, tls_mode_label) for a probe. Skip TLS verify when explicitly
    asked OR for a trusted-internal host (self-hosted LAN box, self-signed cert
    is normal there); public hosts always verify."""
    from .._ssl import auto_relax_internal as _ssl_auto_relax
    is_https = url.lower().startswith("https://")
    auto = (not insecure) and _ssl_auto_relax(url)
    skip_tls = is_https and (insecure or auto)
    if skip_tls:
        tls_mode = "skip-verify(auto-internal)" if auto else "skip-verify(CERT_NONE)"
    elif is_https:
        tls_mode = "verify"
    else:
        tls_mode = "plain-http"
    return skip_tls, tls_mode


def _probe_models(payload) -> list:
    """The model ids from a /models payload's ``data`` list."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]


def probe(base_url: str, api_key: str | None = None,
          timeout: float | None = None, insecure: bool = False) -> dict:
    """Test-connection helper for the home page. GETs ``{base}/models`` and
    returns ``{ok, models: [ids], error?}``. Never raises.

    ``insecure=True`` skips TLS verification for THIS probe only — the operator
    explicitly ticked "skip TLS verify" for a self-signed / internal HTTPS
    endpoint they're deliberately testing. It never relaxes any other host.
    """
    if not base_url or not base_url.strip():
        return {"ok": False, "error": "base_url required", "models": []}
    url = _ensure_v1(base_url.strip()) + "/models"
    headers = {"Accept": _APPLICATION_JSON, "User-Agent": _user_agent()}
    has_token = bool(api_key and api_key.strip() and api_key.strip() != _NO_TOKEN)
    if has_token:
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    skip_tls, tls_mode = _probe_tls_plan(url, insecure)
    # Diagnostic: the EXACT url hit, whether TLS was skipped (and why), token
    # presence (never the token). Grep the API logs for "probe ->".
    log.info("probe -> url=%s insecure_flag=%s tls=%s token=%s",
             url, insecure, tls_mode, "yes" if has_token else "no")
    from .._ssl import context_for as _ssl_context_for
    from .._ssl import insecure_context as _ssl_insecure
    try:
        # Inside the try so a bad CA bundle path (FileNotFoundError) is reported
        # as a clean {ok: False, error} instead of raising.
        ctx = _ssl_insecure() if skip_tls else _ssl_context_for(url)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(
                req, timeout=_probe_timeout(timeout), context=ctx) as r:
            payload = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        log.warning("probe FAILED url=%s tls=%s: %s", url, tls_mode, exc)
        return {"ok": False, "error": str(exc), "models": []}
    log.info("probe OK url=%s tls=%s", url, tls_mode)
    return {"ok": True, "models": _probe_models(payload)}


_NATIVE_TOOL = [{"type": "function", "function": {
    "name": "aiforge_ping",
    "description": "Acknowledge readiness by echoing ack.",
    "parameters": {"type": "object",
                   "properties": {"ack": {"type": "string"}},
                   "required": []}}}]


def probe_native(base_url: str, model: str, api_key: str | None = None,
                 timeout: float | None = None, insecure: bool = False) -> dict:
    """Native tool-calling test-connection. POSTs a real one-tool request to
    ``{base_url}/chat/completions`` — the EXACT url + shape the chat loop uses
    (no ``_ensure_v1`` rewrite, unlike :func:`probe`) — under both
    ``tool_choice`` modes, and reports what the endpoint actually returned so an
    operator can SEE why native FC fails: a ``tool_calls`` reply (native works),
    plain ``content`` (the model ignored the tool), or the error body/HTTP code.
    Never raises.

    This is what the Settings "Test native tools" button calls. It answers the
    exact question ``_native._probe_native`` hides behind an optimistic default:
    does THIS model, at THIS url, return tool_calls for a tools request?
    """
    import urllib.error
    if not base_url or not base_url.strip():
        return {"ok": False, "error": "base_url required"}
    if not model or not model.strip():
        return {"ok": False, "error": "model required"}
    url = base_url.strip().rstrip("/") + "/chat/completions"
    headers = {"Content-Type": _APPLICATION_JSON,
               "Accept": _APPLICATION_JSON, "User-Agent": _user_agent()}
    if api_key and api_key.strip() and api_key.strip() != _NO_TOKEN:
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    skip_tls, tls_mode = _probe_tls_plan(url, insecure)
    from .._ssl import context_for as _ssl_context_for
    from .._ssl import insecure_context as _ssl_insecure
    ctx = _ssl_insecure() if skip_tls else _ssl_context_for(url)
    log.info("probe_native -> url=%s model=%s tls=%s", url, model, tls_mode)

    results: dict = {}
    for choice in ("auto", "required"):
        body = json.dumps({
            "model": model.strip(),
            "messages": [{"role": "user",
                          "content": "Call the aiforge_ping tool with ack='ok'."}],
            "tools": _NATIVE_TOOL,
            "tool_choice": choice,
            "max_tokens": 64,
        }).encode()
        try:
            req = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST")
            with urllib.request.urlopen(
                    req, timeout=_probe_timeout(timeout), context=ctx) as r:
                payload = json.loads(r.read())
            ch0 = (payload.get("choices") or [{}])[0]
            msg = ch0.get("message") or {}
            results[choice] = {
                "ok": True,
                "tool_calls": bool(msg.get("tool_calls")),
                "finish_reason": ch0.get("finish_reason"),
                "content_preview": (msg.get("content") or "")[:160],
            }
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read()[:400].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            results[choice] = {"ok": False, "http": exc.code, "error": err_body}
        except Exception as exc:  # noqa: BLE001
            results[choice] = {"ok": False, "error": str(exc)[:400]}

    got_tool_calls = any(r.get("tool_calls") for r in results.values())
    # "auto" is the mode the RUNTIME uses; "required" is only the probe's forcing
    # mode. Native FC is USABLE iff auto returns a tool_call.
    auto = results.get("auto", {})
    if auto.get("tool_calls"):
        verdict = "native tool-calling works (tool_calls returned for tool_choice=auto)"
    elif auto.get("ok"):
        verdict = ("the endpoint answered but returned plain content, NOT a "
                   "tool_call, for tool_choice=auto — this model ignores tools, "
                   "so native tool-calling will not work here")
    elif got_tool_calls:
        verdict = ("tool_choice=auto failed but tool_choice=required returned a "
                   "tool_call — the endpoint supports tools but not the 'auto' "
                   "mode the runtime uses")
    else:
        verdict = ("no tool_call under either mode — see the error/http fields; "
                   "the endpoint likely rejects the tools payload")
    return {"ok": bool(auto.get("tool_calls")), "model": model.strip(),
            "url": url, "verdict": verdict, "results": results}


register_provider(OpenAICompatibleProvider())
