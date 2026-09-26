"""Is the MODEL there — not just the HTTP server in front of it?

``GET /models`` is not enough: behind a router (a Cloudflare tunnel, a proxy
in front of an MLX/llama.cpp box) ``/models`` answers while the model process
behind it is dead, and every completion 502s. So both probes of
llm/model_wait send a real, minimal completion to the SAME model, through the
same wire the request used:

* an OpenAI-compatible model (a bare id, ``openai/``, ``hosted_vllm/`` …):
  ``POST {base}/chat/completions`` with a one-word prompt and ``max_tokens=1``
  and nothing else (no ``temperature`` — reasoning models reject it); a 400
  that names the token parameter is retried once with
  ``max_completion_tokens=1`` (OpenAI reasoning models);
* a model on another litellm provider (``anthropic/``, ``azure/``,
  ``gemini/``, ``vertex_ai/``, ``bedrock/`` … — the ADK path): the same
  ``litellm.completion`` the request went through, with ``max_tokens=1`` and
  ``drop_params`` (litellm maps it to the provider's own parameter).

Both send the request's own kwargs (:func:`register_send`: TLS relax for a
self-signed proxy, a gateway's headers, the api version, the provider). The
answer is a state of llm/_probe_states — OK, BUSY (incl. a 4xx that says
"loading / not found / swapping"), INCONCLUSIVE (another 4xx), REFUSED (the
host refused / reset: the process is gone) or DOWN (DNS, no route, a proxy's
502) — each carrying its HTTP status.

* :func:`probe` — recovery: is the endpoint serving again (OK or
  INCONCLUSIVE)? A generous timeout: a busy box that answers in 20 s is back.
* :func:`live_state` / :func:`live_probe` — judgement: did the tiny completion
  SUCCEED PROMPTLY — within a timeout proportional to the latency seen on
  earlier probes of that model? A success is cached for
  ``AIFORGE_LLM_PROBE_CACHE_S`` (default 5), so a burst of failures does not
  add a blocking probe to every one of them.

Probes deliberately bypass the rate limiter, the call meter and the slot
accounting: one is sent only after a request failed (or while waiting for a
dead model), costs one token, and is not a generation the step pays for; and
taking a limiter slot for it while the failed request's caller still holds
one would deadlock a one-slot endpoint — the probe that decides whether to
resend would queue behind the request it is judging.

  AIFORGE_LLM_LIVE_PROBE_S  the prompt-answer bound before any latency was
                            seen, and its floor after (default 10).
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

_LAT: dict = {}
_LAT_LOCK = threading.Lock()

#: Recovery probe timeout: long enough for a busy box to squeeze a 1-token
#: answer in; not a limit on waiting (the wait loop probes again after it).
RECOVERY_TIMEOUT_S = 30.0

from ._probe_states import (  # noqa: E402,F401 — the public names live here too
    ANSWERED,
    BUSY,
    DOWN,
    INCONCLUSIVE,
    OK,
    REFUSED,
    State,
    exc_state,
    status_state,
)

#: litellm prefixes that speak the OpenAI wire: probed with plain HTTP.
_OPENAI_WIRE = ("openai", "hosted_vllm", "lm_studio", "openai_like",
                "custom_openai", "text-completion-openai")
_OK_AT: dict = {}
#: (base url, model) -> the send kwargs the request itself used (TLS,
#: headers, api version, provider) — the probe must go the same way.
_SEND_KW: dict = {}
_SEND_KEYS = ("ssl_verify", "extra_headers", "api_version",
              "custom_llm_provider", "headers", "organization")


def _base_s() -> float:
    try:
        return max(0.5, float(os.environ.get("AIFORGE_LLM_LIVE_PROBE_S")
                              or 10.0))
    except ValueError:
        return 10.0


def _bare_model(model: str) -> str:
    """litellm's ``openai/<id>`` prefix is not part of the server's id."""
    m = str(model or "")
    for pre in ("openai/", "hosted_vllm/", "lm_studio/", "openai_like/",
                "custom_openai/"):
        if m.startswith(pre):
            return m[len(pre):]
    return m


def live_timeout_s(url: str, model: str = "") -> float:
    """The "promptly" bound: 4x the FASTEST of this model's recent prompt
    answers (its unloaded latency — a slow remote model gets more room), never
    below the base and never above 6x it (a busy box answering slowly must not
    teach the bound to wait for busy answers)."""
    with _LAT_LOCK:
        seen = _LAT.get((str(url or "").rstrip("/"), _bare_model(model)))
        fastest = min(seen) if seen else None
    base = _base_s()
    if fastest is None:
        return base
    return min(6.0 * base, max(base, 4.0 * fastest))


def _note(url: str, model: str, secs: float) -> None:
    key = (str(url or "").rstrip("/"), _bare_model(model))
    with _LAT_LOCK:
        _LAT.setdefault(key, deque(maxlen=20)).append(secs)


def _status_state(code: int, text: str = "") -> State:
    return status_state(code, text)


def _exc_state(exc: BaseException) -> State:
    return exc_state(exc)


def register_send(url: str, model: str, kwargs: dict) -> None:
    """The sender of requests to ``model`` at ``url`` uses these kwargs;
    the probe of that model uses the same (TLS relax for a self-signed
    proxy, a gateway's headers, the api version, the provider)."""
    keep = {k: kwargs[k] for k in _SEND_KEYS if kwargs.get(k) is not None}
    with _LAT_LOCK:
        _SEND_KW[(str(url or "").rstrip("/"), str(model or ""))] = keep


def _send_kw(url: str, model: str) -> dict:
    with _LAT_LOCK:
        return dict(_SEND_KW.get((str(url or "").rstrip("/"),
                                  str(model or ""))) or {})


def _provider(model: str, custom: str = "") -> str:
    """The litellm provider of ``model`` (its prefix, or the request's
    ``custom_llm_provider``) when it is NOT OpenAI wire (and litellm knows
    it), else ""."""
    m = str(model or "")
    if "/" not in m and not custom:
        return ""
    pre = custom or m.split("/", 1)[0]
    if not pre or pre in _OPENAI_WIRE:
        return ""
    try:
        import litellm
        known = {str(getattr(p, "value", p)) for p in litellm.provider_list}
    except Exception:  # noqa: BLE001 — no litellm: nothing else could send it
        return ""
    return pre if pre in known else ""


def _litellm_probe(url: str, api_key: str, model: str,
                   timeout_s: float, send_kw: dict) -> str:
    """The probe through litellm, as the ADK request went (its kwargs)."""
    try:
        import litellm
    except Exception:  # noqa: BLE001
        return State(INCONCLUSIVE)
    kw: dict = {**send_kw, "model": model, "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1, "timeout": timeout_s, "drop_params": True,
                "stream": False, "num_retries": 0}
    if url:
        kw["api_base"] = url
    if api_key:
        kw["api_key"] = api_key
    try:
        litellm.completion(**kw)
    except Exception as exc:  # noqa: BLE001
        return _exc_state(exc)
    return State(OK, 200)


def _post(base: str, api_key: str, body: dict, timeout_s: float,
          headers: dict | None = None, verify: bool = True):
    """``(status, error text)`` of one POST; raises on a transport failure."""
    import urllib.error
    import urllib.request
    try:
        from aiforge_core.llm.user_agent import user_agent
        agent = user_agent()
    except Exception:  # noqa: BLE001
        agent = "aiforge"
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode(),
        method="POST", headers={"Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json",
                                "Accept": "application/json",
                                "User-Agent": agent, **(headers or {})})
    ctx = None
    if base.lower().startswith("https://") and not verify:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif base.lower().startswith("https://"):
        try:
            from aiforge_core.llm._ssl import context_for
            ctx = context_for(base)
        except Exception:  # noqa: BLE001
            ctx = None
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            resp.read(1 << 16)
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(4096).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        return exc.code, text


def _names_token_param(text: str) -> bool:
    t = (text or "").lower()
    return "max_tokens" in t or "max_completion_tokens" in t


def _http_probe(base: str, api_key: str, model: str, timeout_s: float,
                send_kw: dict) -> str:
    body: dict = {"messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 1, "stream": False}
    if model:
        body["model"] = _bare_model(model)
    hdrs = {**(send_kw.get("headers") or {}),
            **(send_kw.get("extra_headers") or {})}
    verify = send_kw.get("ssl_verify") is not False
    try:
        code, text = _post(base, api_key, body, timeout_s, hdrs, verify)
        if code == 400 and _names_token_param(text):
            # An OpenAI reasoning model: its own token parameter.
            body.pop("max_tokens", None)
            body["max_completion_tokens"] = 1
            code, text = _post(base, api_key, body, timeout_s, hdrs, verify)
    except Exception as exc:  # noqa: BLE001 — refused, DNS, timeout, TLS
        return _exc_state(exc)
    return _status_state(code, text)


def completion_probe(url: str, api_key: str = "", model: str = "",
                     timeout_s: float = RECOVERY_TIMEOUT_S,
                     learn: bool = False) -> str:
    """One one-token completion to ``model``: :data:`OK`,
    :data:`INCONCLUSIVE`, :data:`BUSY` or :data:`DOWN`."""
    base = str(url or "").rstrip("/")
    send_kw = _send_kw(base, model)
    provider = _provider(model, str(send_kw.get("custom_llm_provider") or ""))
    if not base and not provider:
        return State(DOWN)
    t0 = time.monotonic()
    if provider:
        state = _litellm_probe(base, api_key, model, timeout_s, send_kw)
    else:
        state = _http_probe(base, api_key, model, timeout_s, send_kw)
    if state == OK and learn:
        _note(base, model, time.monotonic() - t0)
    return state


def _cache_s() -> float:
    try:
        return max(0.0, float(os.environ.get("AIFORGE_LLM_PROBE_CACHE_S")
                              or 5.0))
    except ValueError:
        return 5.0


def probe(url: str, api_key: str = "", timeout_s: float = RECOVERY_TIMEOUT_S,
          model: str = "") -> bool:
    """Is the model serving again — does a tiny completion get an answer
    (OK, or a client-class refusal that does not say "loading")?"""
    return completion_probe(url, api_key, model, timeout_s) in (
        OK, INCONCLUSIVE)


def live_state(url: str, api_key: str = "", model: str = "",
               fresh: bool = False) -> str:
    """The judgement probe's state. A success within the last
    ``AIFORGE_LLM_PROBE_CACHE_S`` is reused unless ``fresh``."""
    import hashlib
    key = (str(url or "").rstrip("/"), str(model or ""),
           hashlib.sha256(str(api_key or "").encode()).hexdigest()[:12])
    if not fresh:
        with _LAT_LOCK:
            at = _OK_AT.get(key)
        if at is not None and time.monotonic() - at < _cache_s():
            return State(OK, 200)
    state = completion_probe(url, api_key, model, live_timeout_s(url, model),
                             learn=True)
    with _LAT_LOCK:
        if state == OK:
            _OK_AT[key] = time.monotonic()
        else:
            _OK_AT.pop(key, None)
    return state


def live_probe(url: str, api_key: str = "", model: str = "",
               state: bool = False, fresh: bool = False):
    """Did a tiny completion to the same model succeed promptly? With
    ``state=True`` the :func:`live_state` answer itself."""
    got = live_state(url, api_key, model, fresh=fresh)
    return got if state else got == OK


def _reset_for_tests() -> None:
    with _LAT_LOCK:
        _LAT.clear()
        _OK_AT.clear()
        _SEND_KW.clear()


__all__ = ["probe", "live_probe", "live_state", "completion_probe",
           "live_timeout_s", "register_send", "OK", "ANSWERED",
           "INCONCLUSIVE", "BUSY", "REFUSED", "DOWN", "RECOVERY_TIMEOUT_S"]
