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


# A provider refusing on ITS OWN rate limit does not always say 429. The
# observed gateway returns HTTP 400 with
#   {"detail": "You've used 20 requests with this model in the last minute,
#    exceeding your limit of 20 requests per minute."}
# and a bare 400 is classified permanent — so the retry loop skipped the
# backoff, bubbled instantly, and the model chain immediately spent ANOTHER
# request on the next model. A rate limit is the most transient failure there
# is: waiting is the entire remedy.
#
# MINUTE-SCALE ONLY. See _QUOTA_MARKERS.
_RATE_LIMIT_MARKERS = (
    "rate limit", "rate_limit", "ratelimit",
    "requests per minute", "exceeding your limit", "too many requests",
)

# NOT the same thing, however similar it reads. A per-day cap, an exhausted
# billing quota (OpenAI's `insufficient_quota` arrives as a 429) or a per-hour
# metric is permanent for hours — waiting a minute cannot fix it. Treating
# these as rate limits meant a dead API key re-armed a process-wide hold on
# every attempt, then again for every model in the chain: minutes of stall per
# call, where it used to fail fast and say why.
_QUOTA_MARKERS = (
    "quota exceeded", "insufficient_quota", "insufficient quota",
    "exceeded your current quota", "requests per day", "requests per hour",
    "billing", "credit balance",
)


def _error_message(body: str) -> str:
    """The server's own message, not the whole envelope.

    Gateways commonly ECHO the request inside the error body, and this text is
    matched against phrases like "rate limit" — so scanning the envelope let a
    user asking the agent about a rate-limit error they had pasted in arm a
    process-wide hold. Narrow to the field the server put its verdict in when
    the body is JSON; fall back to the whole body when it is not (a plain-text
    nginx/Cloudflare page has no field to narrow to).
    """
    txt = (body or "").strip()
    if not txt.startswith("{"):
        return txt
    try:
        import json
        doc = json.loads(txt)
    except Exception:  # noqa: BLE001 — not JSON after all
        return txt
    if not isinstance(doc, dict):
        return txt
    for key in ("detail", "message", "error_description"):
        val = doc.get(key)
        if isinstance(val, str):
            return val
    err = doc.get("error")
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        parts = [str(err.get(k)) for k in ("message", "code", "type")
                 if isinstance(err.get(k), str)]
        if parts:
            return " ".join(parts)
    return txt


def _full_err_body(exc: Exception) -> str:
    """The WHOLE error body, unclipped. ``_http_err_body`` truncates to 600
    chars because it feeds a log line; classification must not, or a gateway
    that echoes the request before its verdict is judged on the echo — the two
    functions then give opposite answers about the same exception."""
    if not isinstance(exc, urllib.error.HTTPError):
        return ""
    raw = getattr(exc, "_aiforge_body", None)
    if raw is None:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            return ""
        try:
            exc._aiforge_body = raw  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    try:
        return raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return str(raw)


def is_rate_limited(exc: Exception) -> bool:
    """True when ``exc`` is a provider saying we are over its rate limit —
    whatever status code it chose to say it with. THE one definition; both the
    classifier below and the structured/instructor transport call this, because
    three copies of this judgement is three chances for them to disagree."""
    if not isinstance(exc, urllib.error.HTTPError):
        return False
    return status_body_is_rate_limited(exc.code, _full_err_body(exc))


def is_quota_exhausted(exc: Exception) -> bool:
    """True when the provider is out of quota/credit rather than merely too
    fast — permanent for hours, so neither retried nor held for."""
    if not isinstance(exc, urllib.error.HTTPError):
        return False
    if not (400 <= exc.code < 500):
        return False
    return body_is_quota_exhausted(_full_err_body(exc))


def body_is_quota_exhausted(body: str) -> bool:
    low = _error_message(body).lower()
    return any(m in low for m in _QUOTA_MARKERS)


def body_is_rate_limited(body: str) -> bool:
    """Does this 4xx body name a MINUTE-SCALE rate limit? Shared with the
    structured path, which sees an httpx response rather than an HTTPError."""
    low = _error_message(body).lower()
    if any(m in low for m in _QUOTA_MARKERS):
        return False
    return any(m in low for m in _RATE_LIMIT_MARKERS)


def status_body_is_rate_limited(status: int, body: str) -> bool:
    """The WHOLE judgement — status half and body half in one place, so the
    wire path and the structured path cannot drift apart on either.

    A 429 is a rate limit by definition UNLESS its body says quota: OpenAI
    returns `insufficient_quota` as a 429, and holding the process for that is
    a minute of stall against a condition a minute cannot clear.
    """
    if status == 429:
        return not body_is_quota_exhausted(body)
    if not (400 <= status < 500):
        return False
    return body_is_rate_limited(body)


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


def _http_body_names_model_drop(exc: "urllib.error.HTTPError") -> bool:
    """Whether a 4xx's body names a model drop (server reloading), making it
    transient rather than a permanent bad-request. ``exc.read()`` is ONE-SHOT —
    prefer a body already read+stashed by another reader (_http_err_body /
    _tools_unsupported may run FIRST and consume it); only read fresh when
    nothing is stashed, and never overwrite a good stash with a second read's
    empty bytes."""
    try:
        body = getattr(exc, "_aiforge_body", None)
        if body is None:
            body = exc.read()
            exc._aiforge_body = body  # type: ignore[attr-defined]
        return bool(body) and any(
            m in body.decode("utf-8", "replace").lower()
            for m in _MODEL_DROP_MARKERS)
    except Exception:  # noqa: BLE001
        return False


def _http_error_transient(exc: "urllib.error.HTTPError") -> tuple[bool, str]:
    """(retry?, label) for an HTTPError. Rate-limit is checked BEFORE the
    _TRANSIENT_HTTP shortcut: 401/403 are in that set (the nginx blip), so a
    gateway answering "quota exceeded" with a 403 (Cloudflare + several API
    gateways do) must not return http_403, take a 0.5s backoff and never tell
    the ceiling — the whole process then kept hammering a server that said stop."""
    if is_rate_limited(exc):
        return True, "rate_limited"
    if exc.code in _TRANSIENT_HTTP:
        return True, f"http_{exc.code}"
    if _http_body_names_model_drop(exc):
        return True, "model_reloading_4xx"
    return False, f"http_{exc.code}"


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
        return _http_error_transient(exc)
    if isinstance(exc, urllib.error.URLError):
        return True, "url_error"
    if isinstance(exc, TimeoutError):
        return True, "timeout"
    if isinstance(exc, OSError):
        return True, "os_error"
    return False, exc.__class__.__name__
