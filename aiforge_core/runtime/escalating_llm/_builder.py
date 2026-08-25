"""Model construction (``_build_one``) + Langfuse pipeline-call mirroring.

Split out of the former single-module ``escalating_llm``; behaviour identical.
"""
from __future__ import annotations

from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest

from ._quieting import log, _install_adk_toolarg_repair


def _resolve_max_tokens() -> int:
    """Operator-tunable generation cap (UI → runtime_settings.json → env →
    default). Too small truncates a doer's file-write tool-call args."""
    try:
        from aiforge_core.config import runtime_settings as _rs
        return _rs.get("max_output_tokens")
    except Exception:  # noqa: BLE001 — never block a build on settings
        import os as _os_mt
        try:
            return int(_os_mt.environ.get("AIFORGE_LLM_MAX_TOKENS", "32768"))
        except ValueError:
            return 32768


def _disable_litellm_tls(api_base: str) -> None:
    """Turn off litellm's GLOBAL ssl verification. litellm's HTTP client reads
    the global ``litellm.ssl_verify`` when it builds (and caches) its connector,
    so the per-call ssl_verify kwarg does NOT reconfigure an already-built one —
    a self-signed internal endpoint still raised CERTIFICATE_VERIFY_FAILED. This
    relaxes verification for litellm process-wide — acceptable for a self-hosted
    deploy whose model endpoint uses an internal/self-signed cert."""
    try:
        import litellm as _ll
        if _ll.ssl_verify is not False:
            _ll.ssl_verify = False
            _ll.disable_aiohttp_transport = True
            import os as _o
            _o.environ.setdefault("SSL_VERIFY", "False")
            log.warning(
                "litellm TLS verification disabled (insecure/internal model "
                "endpoint %s) — set AIFORGE_LLM_CA_BUNDLE to a PEM to keep "
                "verification on.", api_base)
    except Exception:  # noqa: BLE001
        pass


def _maybe_relax_tls(kwargs: dict, cfg: dict, api_base: str) -> None:
    """Skip TLS verify for a self-hosted HTTPS endpoint with an internal cert.

    Applies when the per-role opt-out is set, the global AIFORGE_LLM_SSL_VERIFY
    toggle is off, OR the host is trusted-internal — and NO CA bundle is set (a
    bundle keeps verify ON, and public hosts always verify). Mirrors
    openai_compatible.probe so Test and real calls agree.
    """
    if not str(api_base).lower().startswith("https://"):
        return
    from aiforge_core.llm import _ssl as _llm_ssl
    if _llm_ssl._ca_bundle():
        return
    if not (cfg.get("insecure_tls") or not _llm_ssl._verify_enabled()
            or _llm_ssl.auto_relax_internal(api_base)):
        return
    kwargs["ssl_verify"] = False
    _disable_litellm_tls(api_base)


def _resolve_timeout():
    """A generous READ timeout (self-hosted reasoning models need minutes) with
    a SHORT connect timeout. A scalar timeout applies to BOTH — so an
    unreachable host blocks the full read timeout just to fail the TCP connect,
    which × attempt-retries × the candidate chain compounds into a multi-hour
    retry storm (the "pipeline runs forever" symptom). Both env-tunable; falls
    back to the scalar when httpx is absent."""
    import os as _os
    try:
        read_to = float(_os.environ.get("AIFORGE_LLM_TIMEOUT_S", "900"))
    except ValueError:
        read_to = 900.0
    try:
        connect_to = float(_os.environ.get("AIFORGE_LLM_CONNECT_TIMEOUT_S", "8"))
    except ValueError:
        connect_to = 8.0
    try:
        import httpx as _httpx
        return _httpx.Timeout(read_to, connect=min(connect_to, read_to))
    except Exception:  # noqa: BLE001 — fall back to the scalar if httpx absent
        return read_to


def _build_one(cfg: dict[str, Any]) -> BaseLlm:
    """Construct a BaseLlm from a resolve_litellm-shaped dict.

    Recognised cfg keys (besides ``model_id``/``api_base``/``api_key``):

    * ``custom_llm_provider`` — override LiteLLM's URL/model auto-detect.
      Required for ollama.com (OpenAI-compat at ``/v1`` but LiteLLM
      misroutes to ``/api/generate`` without it).
    """
    from google.adk.models.lite_llm import LiteLlm
    _install_adk_toolarg_repair()
    kwargs: dict[str, Any] = {"model": cfg["model_id"],
                              "max_tokens": _resolve_max_tokens()}
    api_base = cfg.get("api_base") or ""
    if api_base:
        kwargs["api_base"] = api_base
    if cfg.get("api_key"):
        kwargs["api_key"] = cfg["api_key"]
    if cfg.get("custom_llm_provider"):
        kwargs["custom_llm_provider"] = cfg["custom_llm_provider"]
    _maybe_relax_tls(kwargs, cfg, api_base)
    kwargs["timeout"] = _resolve_timeout()
    # Same agent as the direct client: the pipeline is the higher-volume path,
    # so a gateway that only saw the client's header would have been reading the
    # smaller half of the traffic.
    from aiforge_core.llm.user_agent import user_agent as _ua
    kwargs["extra_headers"] = {"User-Agent": _ua()}
    # ADK's LiteLlm hardcodes ``stream_options={"include_usage": True}`` on
    # every streaming completion. Strict OpenAI-compatible proxies then reject
    # the request with "Stream options can only be defined when stream=True".
    # stream_options only carries usage-in-stream accounting, so drop it at the
    # litellm layer; ``drop_params`` additionally sheds any other unaccepted
    # param.
    kwargs["drop_params"] = True
    kwargs["additional_drop_params"] = ["stream_options"]
    return LiteLlm(**kwargs)


def _lf_request_messages(llm_request: LlmRequest) -> list[dict]:
    """Flatten an ADK request into ``[{role, content}]`` — the system
    instruction first, then each content's concatenated part text."""
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
    return msgs


def _lf_response_text(responses: list) -> str:
    """The concatenated text of every part across ADK responses."""
    out = ""
    for r in responses:
        cont = getattr(r, "content", None)
        for p in (getattr(cont, "parts", None) or []):
            out += getattr(p, "text", "") or ""
    return out


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
        try:
            from aiforge_core.runtime.request_context import get_session_id
            sid = get_session_id()
        except Exception:  # noqa: BLE001
            sid = None
        _lf.record_generation(role=role, model=model_name,
                              messages=_lf_request_messages(llm_request),
                              output=_lf_response_text(responses),
                              latency_ms=latency_ms, session_id=sid,
                              metadata={"path": "pipeline"})
    except Exception:  # noqa: BLE001 — tracing must never break a call
        pass
