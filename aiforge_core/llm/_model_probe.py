"""Is the MODEL there — not just the HTTP server in front of it?

``GET /models`` is not enough: behind a router (a Cloudflare tunnel, a proxy
in front of an MLX/llama.cpp box) ``/models`` answers while the model process
behind it is dead, and every completion 502s. So both probes of
llm/model_wait send a real, minimal completion to the SAME model
(``max_tokens=1``, a one-word prompt):

* :func:`probe` — recovery: does the endpoint ANSWER a completion at all (any
  status below 500 other than 408/429 — a 400 for a probe the server does not
  like still says "a live server is processing requests")? A generous
  timeout: a busy box that answers in 20 s is back.
* :func:`live_probe` — judgement: did the tiny completion SUCCEED (2xx)
  PROMPTLY — within a timeout proportional to the latency seen on earlier
  probes of that model? Only then is a failure of the big request that
  request's fault; a probe that fails, queues or times out means the model is
  down or busy, and that is waited for, never counted.

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

OK = "ok"               # 2xx: the model generated
ANSWERED = "answered"   # a non-5xx refusal: a live server handled the request
DOWN = "down"           # 5xx / 408 / 429 / timeout / refused


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


def completion_probe(url: str, api_key: str = "", model: str = "",
                     timeout_s: float = RECOVERY_TIMEOUT_S,
                     learn: bool = False) -> str:
    """One ``max_tokens=1`` completion: :data:`OK`, :data:`ANSWERED` or
    :data:`DOWN`."""
    import urllib.error
    import urllib.request
    base = str(url or "").rstrip("/")
    if not base:
        return DOWN
    body: dict = {"messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 1, "stream": False, "temperature": 0}
    if model:
        body["model"] = _bare_model(model)
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode(),
        method="POST", headers={"Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json",
                                "Accept": "application/json"})
    ctx = None
    if base.lower().startswith("https://"):
        try:
            from aiforge_core.llm._ssl import context_for
            ctx = context_for(base)
        except Exception:  # noqa: BLE001
            ctx = None
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            resp.read(1 << 16)
    except urllib.error.HTTPError as exc:
        if exc.code >= 500 or exc.code in (408, 429):
            return DOWN
        return ANSWERED
    except Exception:  # noqa: BLE001 — refused, DNS, timeout, TLS: down/busy
        return DOWN
    if learn:
        _note(base, model, time.monotonic() - t0)
    return OK


def probe(url: str, api_key: str = "", timeout_s: float = RECOVERY_TIMEOUT_S,
          model: str = "") -> bool:
    """Is the model back — does a tiny completion get an answer at all?"""
    return completion_probe(url, api_key, model, timeout_s) != DOWN


def live_probe(url: str, api_key: str = "", model: str = "") -> bool:
    """Did a tiny completion to the same model succeed promptly?"""
    return completion_probe(url, api_key, model, live_timeout_s(url, model),
                            learn=True) == OK


def _reset_for_tests() -> None:
    with _LAT_LOCK:
        _LAT.clear()


__all__ = ["probe", "live_probe", "completion_probe", "live_timeout_s",
           "OK", "ANSWERED", "DOWN", "RECOVERY_TIMEOUT_S"]
