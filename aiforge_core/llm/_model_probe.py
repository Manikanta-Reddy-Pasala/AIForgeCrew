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

The answer is one of four states:

* :data:`OK` — the model generated (2xx);
* :data:`INCONCLUSIVE` — a client-class refusal (400/401/403/404/…): the
  server is up and handling requests, but the probe proves nothing about the
  model. The judge falls back to counting the request's failure (the old
  ``/models`` behaviour), so the "LLM issue" stop still works;
* :data:`BUSY` — a timeout, 408/429, or a 5xx other than 502: a live server
  that could not answer now (busy, loading, queue full). Waited for;
* :data:`DOWN` — refused / reset / DNS / 502: nothing is serving the model.

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

OK = "ok"                        # 2xx: the model generated
INCONCLUSIVE = "inconclusive"    # 4xx refusal: server up, model unknown
ANSWERED = INCONCLUSIVE          # the old name
BUSY = "busy"                    # timeout / 408 / 429 / 5xx (not 502)
DOWN = "down"                    # refused / reset / DNS / 502

#: litellm prefixes that speak the OpenAI wire: probed with plain HTTP.
_OPENAI_WIRE = ("openai", "hosted_vllm", "lm_studio", "openai_like",
                "custom_openai", "text-completion-openai")
_OK_AT: dict = {}


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


def _status_state(code: int) -> str:
    if 200 <= code < 300:
        return OK
    if code == 502:
        return DOWN
    if code >= 500 or code in (408, 429):
        return BUSY
    return INCONCLUSIVE


def _exc_state(exc: BaseException) -> str:
    """A transport failure (or a provider SDK's exception) as a state."""
    links = [exc] + [x for x in (getattr(exc, "reason", None), exc.__cause__,
                                 exc.__context__)
                     if isinstance(x, BaseException)]
    names = " ".join(type(x).__name__.lower() for x in links)
    text = " ".join(str(x).lower() for x in links)
    if any(isinstance(x, TimeoutError) for x in links) \
            or "timeout" in names or "timed out" in text:
        return BUSY
    if "connection" in names or any(isinstance(x, OSError) for x in links):
        return DOWN               # refused / reset / DNS (litellm says 500)
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and 100 <= code < 600:
        return _status_state(code)
    return DOWN


def _provider(model: str) -> str:
    """The litellm provider prefix of ``model`` when it is NOT OpenAI wire
    (and litellm knows it), else ""."""
    m = str(model or "")
    if "/" not in m:
        return ""
    pre = m.split("/", 1)[0]
    if not pre or pre in _OPENAI_WIRE:
        return ""
    try:
        import litellm
        known = {str(getattr(p, "value", p)) for p in litellm.provider_list}
    except Exception:  # noqa: BLE001 — no litellm: nothing else could send it
        return ""
    return pre if pre in known else ""


def _litellm_probe(url: str, api_key: str, model: str,
                   timeout_s: float) -> str:
    """The probe through litellm, as the ADK request went."""
    try:
        import litellm
    except Exception:  # noqa: BLE001
        return INCONCLUSIVE
    kw: dict = {"model": model, "messages": [{"role": "user", "content": "hi"}],
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
    return OK


def _post(base: str, api_key: str, body: dict, timeout_s: float):
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
                                "User-Agent": agent})
    ctx = None
    if base.lower().startswith("https://"):
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


def _http_probe(base: str, api_key: str, model: str, timeout_s: float) -> str:
    body: dict = {"messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 1, "stream": False}
    if model:
        body["model"] = _bare_model(model)
    try:
        code, text = _post(base, api_key, body, timeout_s)
        if code == 400 and _names_token_param(text):
            # An OpenAI reasoning model: its own token parameter.
            body.pop("max_tokens", None)
            body["max_completion_tokens"] = 1
            code, text = _post(base, api_key, body, timeout_s)
    except Exception as exc:  # noqa: BLE001 — refused, DNS, timeout, TLS
        return _exc_state(exc)
    return _status_state(code)


def completion_probe(url: str, api_key: str = "", model: str = "",
                     timeout_s: float = RECOVERY_TIMEOUT_S,
                     learn: bool = False) -> str:
    """One one-token completion to ``model``: :data:`OK`,
    :data:`INCONCLUSIVE`, :data:`BUSY` or :data:`DOWN`."""
    base = str(url or "").rstrip("/")
    provider = _provider(model)
    if not base and not provider:
        return DOWN
    t0 = time.monotonic()
    if provider:
        state = _litellm_probe(base, api_key, model, timeout_s)
    else:
        state = _http_probe(base, api_key, model, timeout_s)
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
    (OK, or a client-class refusal from a live server)?"""
    return completion_probe(url, api_key, model, timeout_s) in (
        OK, INCONCLUSIVE)


def live_state(url: str, api_key: str = "", model: str = "",
               fresh: bool = False) -> str:
    """The judgement probe's state. A success within the last
    ``AIFORGE_LLM_PROBE_CACHE_S`` is reused unless ``fresh``."""
    key = (str(url or "").rstrip("/"), _bare_model(model))
    if not fresh:
        with _LAT_LOCK:
            at = _OK_AT.get(key)
        if at is not None and time.monotonic() - at < _cache_s():
            return OK
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


__all__ = ["probe", "live_probe", "live_state", "completion_probe",
           "live_timeout_s", "OK", "ANSWERED", "INCONCLUSIVE", "BUSY", "DOWN",
           "RECOVERY_TIMEOUT_S"]
