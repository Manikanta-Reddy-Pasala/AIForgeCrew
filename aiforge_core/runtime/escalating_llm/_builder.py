"""Model construction (``_build_one``) + Langfuse pipeline-call mirroring.

Split out of the former single-module ``escalating_llm``; behaviour identical.
"""
from __future__ import annotations

from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest

from ._quieting import log, _install_adk_toolarg_repair


def _build_one(cfg: dict[str, Any]) -> BaseLlm:
    """Construct a BaseLlm from a resolve_litellm-shaped dict.

    Recognised cfg keys (besides ``model_id``/``api_base``/``api_key``):

    * ``custom_llm_provider`` — override LiteLLM's URL/model auto-detect.
      Required for ollama.com (OpenAI-compat at ``/v1`` but LiteLLM
      misroutes to ``/api/generate`` without it).
    """
    from google.adk.models.lite_llm import LiteLlm
    _install_adk_toolarg_repair()
    kwargs: dict[str, Any] = {"model": cfg["model_id"]}
    # Generous output budget so a tool call carrying file content isn't
    # truncated mid-string (→ malformed JSON args). Tunable; some endpoints
    # cap it, so keep it overridable.
    # Operator-tunable generation cap (UI → runtime_settings.json → env →
    # default). Too small truncates a doer's file-write tool-call args.
    try:
        from aiforge_core.config import runtime_settings as _rs
        kwargs["max_tokens"] = _rs.get("max_output_tokens")
    except Exception:  # noqa: BLE001 — never block a build on settings
        import os as _os_mt
        try:
            kwargs["max_tokens"] = int(
                _os_mt.environ.get("AIFORGE_LLM_MAX_TOKENS", "32768"))
        except ValueError:
            kwargs["max_tokens"] = 32768
    api_base = cfg.get("api_base") or ""
    if api_base:
        kwargs["api_base"] = api_base
    if cfg.get("api_key"):
        kwargs["api_key"] = cfg["api_key"]
    if cfg.get("custom_llm_provider"):
        kwargs["custom_llm_provider"] = cfg["custom_llm_provider"]
    # Self-hosted HTTPS endpoint with a self-signed / internal cert: mirror
    # the urllib client's AIFORGE_LLM_SSL_VERIFY toggle for the LiteLLM
    # (ADK / Team-flow) path. LiteLLM passes ssl_verify through to its
    # httpx client; only relevant for https, only when explicitly disabled.
    # A custom CA bundle (AIFORGE_LLM_CA_BUNDLE / SSL_CERT_FILE /
    # REQUESTS_CA_BUNDLE) is honoured by httpx natively and keeps verify ON.
    if str(api_base).lower().startswith("https://"):
        from aiforge_core.llm import _ssl as _llm_ssl
        # Skip TLS verify for the model endpoint when: the per-role opt-out
        # is set (UI checkbox / stored insecure_tls), the global
        # AIFORGE_LLM_SSL_VERIFY toggle is off, OR the host is trusted-
        # internal (self-hosted LAN box). A CA bundle keeps verify ON, and
        # public hosts always verify. Mirrors openai_compatible.probe so
        # Test and real calls agree.
        if not _llm_ssl._ca_bundle() and (
            cfg.get("insecure_tls")
            or not _llm_ssl._verify_enabled()
            or _llm_ssl.auto_relax_internal(api_base)
        ):
            kwargs["ssl_verify"] = False
            # litellm's HTTP client reads the GLOBAL `litellm.ssl_verify`
            # when it builds (and caches) its aiohttp/httpx connector — the
            # per-call ssl_verify kwarg above does NOT reconfigure an
            # already-built connector, so a self-signed internal endpoint
            # still raised CERTIFICATE_VERIFY_FAILED. Set the global here
            # (pipeline-build time, before the first completion) so the
            # connector is built with verification off. Also force httpx
            # (disable the aiohttp transport) where ssl_verify is honoured
            # most predictably. NOTE: this relaxes verification for litellm
            # globally in this process — acceptable for a self-hosted deploy
            # whose model endpoint uses an internal/self-signed cert.
            try:
                import litellm as _ll
                if _ll.ssl_verify is not False:
                    _ll.ssl_verify = False
                    _ll.disable_aiohttp_transport = True
                    import os as _o
                    _o.environ.setdefault("SSL_VERIFY", "False")
                    log.warning(
                        "litellm TLS verification disabled (insecure/internal "
                        "model endpoint %s) — set AIFORGE_LLM_CA_BUNDLE to a "
                        "PEM to keep verification on.", api_base)
            except Exception:  # noqa: BLE001
                pass
    # Match the urllib client path: a generous request timeout (self-hosted
    # reasoning models need minutes) and a non-default User-Agent (some
    # proxies/WAFs reject httpx/litellm's default). Both env-tunable. Applied
    # to the team-flow / ticket pipeline (LiteLLM) so it agrees with simple
    # chat (client._post).
    import os as _os
    try:
        _read_to = float(_os.environ.get("AIFORGE_LLM_TIMEOUT_S", "900"))
    except ValueError:
        _read_to = 900.0
    # Split connect from read. A scalar timeout applies to BOTH — so an
    # unreachable/asleep host (dropped SYN, no RST) blocks the full read
    # timeout (600s) just to fail the TCP connect, and with 3 attempt-retries
    # × the candidate chain × node-level RetryConfig that compounds into a
    # multi-HOUR retry storm that freezes the single-shot ticket runner (the
    # "pipeline runs forever" symptom). A short CONNECT timeout fails an
    # unreachable endpoint in seconds so escalation moves on immediately,
    # while the generous READ timeout still lets a live reasoning model think
    # for minutes. litellm forwards httpx.Timeout natively.
    try:
        _connect_to = float(_os.environ.get("AIFORGE_LLM_CONNECT_TIMEOUT_S", "8"))
    except ValueError:
        _connect_to = 8.0
    try:
        import httpx as _httpx
        kwargs["timeout"] = _httpx.Timeout(
            _read_to, connect=min(_connect_to, _read_to))
    except Exception:  # noqa: BLE001 — fall back to the scalar if httpx absent
        kwargs["timeout"] = _read_to
    # Same agent as the direct client: the pipeline is the higher-volume path,
    # so a gateway that only saw the client's header would have been reading
    # the smaller half of the traffic.
    from aiforge_core.llm.user_agent import user_agent as _ua
    kwargs["extra_headers"] = {"User-Agent": _ua()}
    # ADK's LiteLlm hardcodes ``stream_options={"include_usage": True}`` on
    # every streaming completion. Strict OpenAI-compatible proxies (e.g. a
    # self-hosted gateway that buffers and drops ``stream:true``) then reject
    # the request with "Stream options can only be defined when stream=True".
    # stream_options only carries usage-in-stream accounting, so drop it at
    # the litellm layer for all attempts. ``drop_params`` additionally lets
    # litellm silently shed any other param the endpoint doesn't accept.
    kwargs["drop_params"] = True
    kwargs["additional_drop_params"] = ["stream_options"]
    return LiteLlm(**kwargs)


def _mirror_to_langfuse(role: str, llm_request: LlmRequest,
                        responses: list, model_name: str,
                        latency_ms: int) -> None:
    """Mirror one PIPELINE model call to Langfuse (chat goes through
    llm.client.complete which mirrors itself — ADK agents come through
    HERE instead, so without this only simple chat showed up). Extracts
    plain text from the ADK request/response shapes; soft-fails."""
    try:
        from aiforge_core.integrations import langfuse_adapter as _lf
        if not _lf.enabled():
            return
        msgs: list[dict] = []
        sys_i = getattr(getattr(llm_request, "config", None),
                        "system_instruction", None)
        if sys_i:
            msgs.append({"role": "system", "content": str(sys_i)})
        for c in getattr(llm_request, "contents", None) or []:
            txt = "".join(getattr(p, "text", "") or ""
                          for p in (getattr(c, "parts", None) or []))
            if txt:
                msgs.append({"role": getattr(c, "role", "user") or "user",
                             "content": txt})
        out = ""
        for r in responses:
            cont = getattr(r, "content", None)
            for p in (getattr(cont, "parts", None) or []):
                out += getattr(p, "text", "") or ""
        try:
            from aiforge_core.runtime.request_context import get_session_id
            _sid = get_session_id()
        except Exception:  # noqa: BLE001
            _sid = None
        _lf.record_generation(role=role, model=model_name, messages=msgs,
                              output=out, latency_ms=latency_ms,
                              session_id=_sid, metadata={"path": "pipeline"})
    except Exception:  # noqa: BLE001 — tracing must never break a call
        pass
