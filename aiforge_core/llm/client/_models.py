"""What models does this endpoint actually serve?

A local OpenAI-compatible server answers a request for a model it does not
have with a 400 whose message is model-lifecycle wording — LM Studio says
``"No models loaded. Please load a model in the developer page or use the 'lms
load' command."``. That is the SAME sentence it uses when a model was
idle-unloaded and will JIT-reload, which is why the classifier treats it as
transient and retries.

For an idle-unload that is right. For a role pointed at a model id the server
has never heard of it is exactly wrong, and the cost is not one wasted call:
every attempt re-ships the whole prompt, three transport attempts per call, and
the chat loop retries five more times on top. A misconfigured role produced
thousands of 400s and an on-screen "the model didn't respond", which names
neither the model nor the endpoint — so the operator debugs the network instead
of the one line of config that is wrong.

This asks the endpoint what it serves, so the failure can say which model was
asked for, where, and what was available instead. It is a DIAGNOSTIC, consulted
once a call chain has already failed — never on the hot path, and never as a
gate that decides whether to send a request.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from ._helpers import _float_env, _log

# base_url -> (expires_at, ids). Short TTL: a model list changes when someone
# loads a model, and the whole point is to be right about a box being fixed.
_TTL_S = 60.0
_cache: dict = {}
_lock = threading.Lock()


def served_models(base_url: str, api_key: str = "",
                  timeout_s: float | None = None) -> "list[str] | None":
    """Model ids this endpoint lists, or ``None`` when it could not be asked.

    ``None`` and ``[]`` mean different things and the caller must not conflate
    them: ``None`` is "no answer" (the probe itself failed — never conclude
    anything about the configured model from it), ``[]`` is "the server says it
    serves nothing".
    """
    url = str(base_url or "").rstrip("/")
    if not url:
        return None
    now = time.monotonic()
    with _lock:
        hit = _cache.get(url)
        if hit and hit[0] > now:
            return hit[1]
    ids: "list[str] | None" = None
    try:
        req = urllib.request.Request(
            f"{url}/models",
            headers={"Authorization": f"Bearer {api_key}",
                     "Accept": "application/json"},
        )
        _t = timeout_s if timeout_s is not None else _float_env(
            "AIFORGE_LLM_MODELS_TIMEOUT_S", 5.0)
        with urllib.request.urlopen(req, timeout=_t) as resp:
            body = json.loads(resp.read())
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, list):
            ids = [str(m.get("id")) for m in data
                   if isinstance(m, dict) and m.get("id")]
    except (OSError, ValueError) as exc:
        # An endpoint that will not answer /v1/models is not evidence about the
        # configured model — leave `ids` None and let the caller say nothing.
        _log.debug("llm.models_probe_failed url=%s err=%s", url, str(exc)[:200])
        ids = None
    with _lock:
        _cache[url] = (now + _TTL_S, ids)
    return ids


def model_is_missing(base_url: str, model: str,
                     api_key: str = "") -> "list[str] | None":
    """The served list when ``model`` is NOT on it, else ``None``.

    Returning the list rather than a bool is deliberate: the only useful thing
    to say about a missing model is what the box has instead.
    """
    ids = served_models(base_url, api_key)
    if ids is None or not model:
        return None
    return None if str(model) in ids else ids


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def pick_substitute(model: str, served: "list[str]") -> "str | None":
    """Which of the endpoint's own models should stand in for ``model``?

    The configured id is the best statement of intent available, so the pick is
    the one that shares the most of it: ``qwen/qwen3.6-27b`` prefers
    ``qwen/qwen3-coder-next`` over an unrelated ``llama-3``. Deterministic —
    the same box picks the same stand-in every call, because a substitution
    that moves around is worse than one that is merely imperfect: nobody can
    reproduce a bug that ran on a different model each time.

    Returns None when there is nothing to pick.
    """
    if not served:
        return None
    want = str(model or "").lower()

    def _shared(cand: str) -> tuple:
        c = cand.lower()
        n = 0
        for a, b in zip(want, c):
            if a != b:
                break
            n += 1
        # Prefix length first, then a stable tiebreak (shorter id, then
        # alphabetical) so the choice never depends on dict/list order.
        return (n, -len(c), [-ord(ch) for ch in c])

    return max(served, key=_shared)


# Marker set on the exhausted-call error when the endpoint does not serve the
# configured model. Callers ABOVE the client (the chat loop's own retry sweep)
# read it the way they read TIMEOUT_SHIPPED_ATTR: a fix at this layer is undone
# one layer up if the loop just re-issues the same impossible call five times.
MODEL_MISSING_ATTR = "aiforge_llm_model_missing"


def model_missing(exc: BaseException) -> bool:
    """True when ``exc`` says the configured model is not served there."""
    return bool(getattr(exc, MODEL_MISSING_ATTR, False))


__all__ = ["served_models", "model_is_missing", "pick_substitute",
           "reset_cache", "MODEL_MISSING_ATTR", "model_missing"]
