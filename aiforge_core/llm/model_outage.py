"""Is this model failure an OUTAGE (wait for the model) or not (report it)?

THE one place that decides it. :mod:`aiforge_core.llm.model_wait` blocks on an
outage until the endpoint answers again; everything else keeps its old
behaviour — a configuration error fails with its clear message, a prompt the
model already received is not re-sent.

Verdicts (:func:`classify`):

``outage`` — the model server cannot serve anyone right now. Waiting fixes it.
  * connect refused / reset / aborted, host or network unreachable, DNS
    failure, connect timeout, the endpoint breaker being open
    ("LLM endpoint unreachable");
  * a stalled stream (:class:`LLMStreamStalled`) — the call was cut, nothing
    is still generating;
  * an unshipped timeout (the request never reached the model);
  * HTTP 408, 502, 503, 504;
  * HTTP 429 that is a minute-scale rate limit (not a quota);
  * model-lifecycle wording at any status but 401/403 — "no models loaded",
    "model not loaded", "model is loading", "unloaded", "not ready" (LM Studio
    idle-unload, JIT loading, a restart) — UNLESS the endpoint answers
    ``/v1/models`` with a non-empty list that lacks the configured model: then
    the id is simply wrong (``config``).

``config`` — waiting cannot fix it; fail with the message as before.
  * 401 / 403 (auth), 400 (bad request), 404 / "model not found" / "unknown
    model" (the id is wrong), a quota or billing cap, the client's own
    ``model_missing`` verdict when the endpoint serves other models.

``shipped`` — a read timeout on a request the model RECEIVED. The server is up
  and still generating; re-sending would start a second generation. Kept as is.

``cancelled`` — Stop / cancel. Never waited for.

``llm_issue`` — :class:`LLMRequestFailing`: the endpoint answers, but THIS
  request failed the same way several times in a row (see
  :mod:`aiforge_core.llm.request_health`). An LLM issue, reported — never
  waited for again.

``other`` — anything else (a 500 with no lifecycle wording, a malformed answer,
  a bug). Existing retry/escalation behaviour applies; no outage wait.
"""
from __future__ import annotations

import errno
import urllib.error

OUTAGE = "outage"
CONFIG = "config"
SHIPPED = "shipped"
CANCELLED = "cancelled"
OTHER = "other"
LLM_ISSUE = "llm_issue"


class LLMRequestFailing(RuntimeError):
    """The model endpoint is UP (it answers ``/models``) but this one request
    keeps failing — a prompt it cannot prefill in time, a server that crashes
    on it, a proxy that always times it out. Waiting cannot fix that, so it is
    an LLM ISSUE: the turn/ticket stops with this, instead of re-sending the
    same request forever. ``reason`` is ``llm_request_fails``."""

    reason = "llm_request_fails"

    def __init__(self, url: str, failures: int, last: "BaseException | None",
                 message: str = "") -> None:
        self.url = url
        self.failures = failures
        self.last = last
        if not message:
            what = (str(last).strip().splitlines() or [""])[0][:200] \
                or type(last).__name__
            message = (f"LLM issue: the model at {url or '?'} is up but "
                       f"failed this request {failures} times in a row ({what})")
        super().__init__(message)

_OUTAGE_STATUS = frozenset({408, 502, 503, 504})
_AUTH_STATUS = frozenset({401, 403})
_CONFIG_STATUS = frozenset({400, 404, 405, 413, 422})

#: The model is (re)loading or was unloaded: transient on every local server.
_LOADING_MARKERS = (
    "no models loaded", "model not loaded", "not loaded", "model is loading",
    "loading model", "model not ready", "still loading", "unloaded",
    "model loading", "is being loaded",
)
#: The configured id is wrong: permanent.
_UNKNOWN_MODEL_MARKERS = (
    "model not found", "model_not_found", "does not exist", "unknown model",
    "invalid model",
)
_RESET_ERRNOS = frozenset({errno.ECONNRESET, errno.EPIPE, errno.ECONNABORTED})
#: Phrases (litellm and friends flatten the socket error into text).
_DOWN_PHRASES = (
    "connection reset", "connection aborted", "remote end closed connection",
    "remotedisconnected", "server disconnected", "broken pipe",
    "service unavailable", "serviceunavailable", "bad gateway",
    "gateway timeout", "temporarily unavailable", "overloaded",
    "endpoint unreachable",
)
#: litellm exception class names with a clear meaning. NOT
#: ``APIConnectionError``: litellm also raises it for failures after the
#: connection is up (a malformed answer), which waiting would loop on forever —
#: it counts only when its text or cause names a connect failure.
_OUTAGE_CLASSES = ("serviceunavailableerror", "llmstreamstalled")
_CONFIG_CLASSES = ("authenticationerror", "permissiondeniederror",
                   "notfounderror", "badrequesterror",
                   "contextwindowexceedederror", "unprocessableentityerror")


def chain(exc: BaseException | None, limit: int = 8) -> list:
    """``exc`` and what it wraps: the client's ``transport_error`` attribute,
    then ``__cause__`` / ``__context__``, and a URLError's ``reason``."""
    seen: list = []
    while exc is not None and len(seen) < limit and all(exc is not s for s in seen):
        seen.append(exc)
        nxt = getattr(exc, "transport_error", None)
        if nxt is None and isinstance(exc, urllib.error.URLError) \
                and isinstance(getattr(exc, "reason", None), BaseException):
            nxt = exc.reason
        exc = nxt or exc.__cause__ or exc.__context__
    return seen


def _text(exc: BaseException) -> str:
    body = ""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            from aiforge_core.llm.client._errors import _full_err_body
            body = _full_err_body(exc)
        except Exception:  # noqa: BLE001
            body = ""
    return (type(exc).__name__ + " " + str(exc) + " " + body).lower()


def _status(exc: BaseException) -> int | None:
    for attr in ("code", "status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and 100 <= val < 600:
            return val
    return None


def _is_cancel(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in ("_LLMCancelled", "ModelWaitCancelled", "CancelledError"):
        return True
    return False


def _is_shipped(exc: BaseException) -> bool:
    try:
        from aiforge_core.llm.client._http_stream import TIMEOUT_SHIPPED_ATTR
        return bool(getattr(exc, TIMEOUT_SHIPPED_ATTR, False))
    except Exception:  # noqa: BLE001
        return False


def _status_verdict(status: int, text: str) -> str | None:
    if status in _AUTH_STATUS:
        return CONFIG
    if status == 429:
        from aiforge_core.llm.client._errors import _QUOTA_MARKERS
        return CONFIG if any(m in text for m in _QUOTA_MARKERS) else OUTAGE
    if status in _OUTAGE_STATUS:
        return OUTAGE
    if any(m in text for m in _LOADING_MARKERS):
        return OUTAGE
    if status in _CONFIG_STATUS or 400 <= status < 500:
        return CONFIG
    return None                       # a 500 without lifecycle wording


def _link_verdict(exc: BaseException) -> str | None:
    """The verdict one link of the chain gives, or None to keep walking."""
    from aiforge_core.llm.client._models import model_missing
    if model_missing(exc):
        served = getattr(exc, "served_models", None)
        return OUTAGE if served == [] else CONFIG
    text = _text(exc)
    status = _status(exc)
    if status is not None:
        verdict = _status_verdict(status, text)
        if verdict is not None:
            return verdict
    cls = type(exc).__name__.lower()
    if cls in _CONFIG_CLASSES:
        return OUTAGE if any(m in text for m in _LOADING_MARKERS) else CONFIG
    if cls == "_modelreloading" or any(m in text for m in _LOADING_MARKERS):
        return OUTAGE
    if any(m in text for m in _UNKNOWN_MODEL_MARKERS):
        return CONFIG
    if cls in _OUTAGE_CLASSES:
        return OUTAGE
    from aiforge_core.llm.endpoint_breaker import is_connect_error
    if is_connect_error(exc):
        return OUTAGE
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                        BrokenPipeError)):
        return OUTAGE
    if isinstance(exc, OSError) and exc.errno in _RESET_ERRNOS:
        return OUTAGE
    if any(p in text for p in _DOWN_PHRASES):
        return OUTAGE
    if isinstance(exc, TimeoutError) or cls in ("timeout", "readtimeout",
                                                 "connecttimeout"):
        return OUTAGE                 # shipped timeouts were excluded above
    return None


def classify(exc: BaseException | None) -> str:
    """``outage`` / ``config`` / ``shipped`` / ``cancelled`` / ``other``."""
    if exc is None:
        return OTHER
    links = chain(exc)
    if any(_is_cancel(e) for e in links[:1]):
        return CANCELLED
    if any(isinstance(e, LLMRequestFailing) for e in links):
        return LLM_ISSUE
    if any(_is_shipped(e) for e in links):
        return SHIPPED
    try:
        for link in links:
            verdict = _link_verdict(link)
            if verdict is not None:
                return verdict
    except Exception:  # noqa: BLE001 — unsure is never an outage
        return OTHER
    return OTHER


def is_outage(exc: BaseException | None) -> bool:
    return classify(exc) == OUTAGE


def issue(exc: BaseException | None) -> "LLMRequestFailing | None":
    """The :class:`LLMRequestFailing` in ``exc``'s chain, or None."""
    for e in chain(exc):
        if isinstance(e, LLMRequestFailing):
            return e
    return None


#: The server itself says "busy / loading, come back later": the endpoint's
#: state, not this request's — always waited for, never counted against it.
_BUSY_STATUS = frozenset({429, 503})


def explicit_busy(exc: BaseException | None) -> bool:
    for e in chain(exc):
        if _status(e) in _BUSY_STATUS or type(e).__name__.lower() in (
                "serviceunavailableerror", "ratelimiterror", "_modelreloading"):
            return True
        if any(m in _text(e) for m in _LOADING_MARKERS):
            return True
    return False


def is_stall(exc: BaseException | None) -> bool:
    return any(type(e).__name__ == "LLMStreamStalled" for e in chain(exc))


def request_bound(exc: BaseException | None) -> bool:
    """Did this failure happen AFTER the server took the request (a stall, a
    gateway timeout, the connection dropped mid-answer)? A connect failure
    never is — it says nothing about the request."""
    for e in chain(exc):
        if type(e).__name__ == "LLMStreamStalled":
            return True
        if _status(e) in (408, 502, 504):
            return True
        if isinstance(e, (ConnectionResetError, ConnectionAbortedError,
                          BrokenPipeError)):
            return True
        text = _text(e)
        if any(p in text for p in ("connection reset", "connection aborted",
                                   "remote end closed", "remotedisconnected",
                                   "server disconnected", "broken pipe")):
            return True
        # A provider SDK's read timeout (litellm on the ADK path): the server
        # had the request. Not our client's bare TimeoutError (pre-send).
        if type(e).__name__.lower() in ("timeout", "apitimeouterror",
                                        "readtimeout") \
                and "connect" not in text:
            return True
    return False


def crash_evidence(exc: BaseException | None) -> bool:
    """Could this failure be the model server itself dying under the
    request? Only its own connection being reset / closed mid-request. Not a
    proxy's 502/504 (the proxy is up, whatever is behind it), not a stall
    (our own bound cut it), not a connect failure or anything on our side."""
    links = chain(exc)
    if any(_status(e) in (408, 429, 502, 503, 504) for e in links):
        return False
    if is_stall(exc):
        return False
    try:
        from aiforge_core.llm import endpoint_breaker
        if endpoint_breaker.is_connect_error(exc):
            return False
    except Exception:  # noqa: BLE001
        return False
    for e in links:
        if isinstance(e, (ConnectionResetError, ConnectionAbortedError,
                          BrokenPipeError)):
            return True
        if type(e).__name__ in ("RemoteDisconnected", "IncompleteRead"):
            return True
        text = _text(e)
        if any(p in text for p in ("connection reset", "remote end closed",
                                   "server disconnected", "connection aborted",
                                   "peer closed", "incomplete read")):
            return True
    return False


def names_loading(exc: BaseException | None) -> bool:
    """Does the failure say the model is not loaded (vs the box being down)?
    Only then is it worth asking ``/v1/models`` whether the id exists at all."""
    return any(any(m in _text(e) for m in _LOADING_MARKERS)
               for e in chain(exc))


__all__ = ["OUTAGE", "CONFIG", "SHIPPED", "CANCELLED", "OTHER", "LLM_ISSUE",
           "LLMRequestFailing", "classify", "is_outage", "issue",
           "explicit_busy", "is_stall", "request_bound", "crash_evidence", "names_loading",
           "chain"]
