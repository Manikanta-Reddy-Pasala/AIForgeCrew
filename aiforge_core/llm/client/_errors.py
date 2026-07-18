"""Client exceptions, transient-error classification, and model-drop detection.
Leaf module — depends only on the stdlib (``os``, ``urllib.error``)."""
from __future__ import annotations

import os
import urllib.error


class _LLMCancelled(Exception):
    """Raised when a post is aborted because its cancel event fired — classified
    non-retryable so the retry loop doesn't re-issue the cancelled call."""


# HTTP status codes that warrant in-place retry (transient): 408 timeout,
# 429 rate-limit, 500/502/503/504 server-side. 401/403 included because the
# self-hosted proxy (nginx) returns intermittent "401 Authorization
# Required" even with a valid token — bounded retries (AIFORGE_LLM_RETRY_MAX)
# ride over the blip instead of failing the chat. Disable the auth retries
# with AIFORGE_LLM_RETRY_AUTH=0 if your endpoint's 401 is always real.
_TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})
if os.environ.get("AIFORGE_LLM_RETRY_AUTH", "1") not in ("0", "false", "no"):
    _TRANSIENT_HTTP = _TRANSIENT_HTTP | {401, 403}


def _http_err_body(exc: Exception) -> str:
    """Best-effort read of an HTTPError response body (the proxy's actual
    rejection detail, e.g. which param it didn't like). urllib's HTTPError
    is a file-like; reading it is one-shot, so guard against re-reads."""
    if not isinstance(exc, urllib.error.HTTPError):
        return ""
    # Prefer a body already read by the classifier (exc.read() is one-shot).
    raw = getattr(exc, "_aiforge_body", None)
    if raw is None:
        try:
            raw = exc.read()
        except Exception:
            return ""
        # Stash so a SECOND reader (retry logging + tools-unsupported classify)
        # gets the same body — the read is one-shot, so without this the order of
        # callers determined whether the body survived (a latent ordering bug).
        try:
            exc._aiforge_body = raw  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    try:
        return raw.decode("utf-8", "replace")[:600]
    except Exception:
        return str(raw)[:600]


# Model-lifecycle phrases a local OpenAI-compatible server emits when the
# model isn't resident (idle-unload / OOM-evict / restart / not-yet-loaded).
# Endpoint-agnostic — mlx-lm, ollama, llama.cpp, vLLM, LM Studio all surface a
# variant. A server may return these as a 200-OK error body OR a 4xx, so we
# match the message text either way and RETRY (gives the server time to
# reload) instead of hard-failing the run.
_MODEL_DROP_MARKERS = (
    "model unloaded", "unloaded", "model not loaded", "not loaded",
    "model not found", "no model", "no models loaded", "model is loading",
    "loading model", "model not ready", "still loading",
)


class _ModelReloading(Exception):
    """Raised when the endpoint reports the model is unloaded/reloading — a
    transient condition that should retry, not fail the run."""


def _raise_if_model_dropped(body: object) -> None:
    """If ``body`` is an OpenAI-style error whose message names a model drop,
    raise :class:`_ModelReloading` so the retry loop re-issues the call."""
    err = body.get("error") if isinstance(body, dict) else None
    if err is None:
        return
    msg = (err.get("message") if isinstance(err, dict) else str(err)) or ""
    low = msg.lower()
    if any(m in low for m in _MODEL_DROP_MARKERS):
        raise _ModelReloading(f"model unavailable (reloading?): {msg[:200]}")


def _is_transient_exc(exc: Exception) -> tuple[bool, str]:
    """Return (retry?, label) for transport exceptions.

    HTTPError 5xx / 408 / 429 → retry (server-side or rate-limit).
    HTTPError 4xx other → no retry, UNLESS its body names a model drop.
    _ModelReloading → retry. URLError / OSError / timeout → retry.
    """
    if isinstance(exc, _LLMCancelled):
        return False, "cancelled"        # don't re-issue an aborted call
    if isinstance(exc, _ModelReloading):
        return True, "model_reloading"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in _TRANSIENT_HTTP:
            return True, f"http_{exc.code}"
        # A 4xx whose body names a model drop is still transient (the server
        # is reloading), not a permanent bad-request. NOTE: exc.read() is
        # one-shot — stash the bytes on the exc so _http_err_body can log the
        # server's actual rejection reason (else the 400 cause is invisible).
        try:
            _body = exc.read()
            exc._aiforge_body = _body  # type: ignore[attr-defined]
            if _body and any(m in _body.decode("utf-8", "replace").lower()
                             for m in _MODEL_DROP_MARKERS):
                return True, "model_reloading_4xx"
        except Exception:  # noqa: BLE001
            pass
        return False, f"http_{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return True, "url_error"
    if isinstance(exc, TimeoutError):
        return True, "timeout"
    if isinstance(exc, OSError):
        return True, "os_error"
    return False, exc.__class__.__name__
