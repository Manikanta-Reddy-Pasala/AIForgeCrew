"""What a liveness probe's answer says (llm/_model_probe, llm/model_wait).

The rule: when the evidence is ambiguous, WAIT. Only a clear answer changes
what the waiter does.

* :data:`OK` — the model generated (2xx);
* :data:`BUSY` — a live server that cannot answer now: a timeout, 408/429,
  a 5xx other than 502, or a 4xx whose text says the model is being
  loaded / pulled / swapped / not found (a router mid-swap answers 404 "model
  not found" for seconds). Waited for;
* :data:`INCONCLUSIVE` — any other 4xx: the server is up, the probe proves
  nothing about the model. Waited for — counted only when the REQUEST failed
  with the same client-error class (see :func:`same_client_error`);
* :data:`REFUSED` — the model server's own host refused / reset the
  connection: the process behind the port is gone. The only probe answer
  that can be evidence of a CRASH;
* :data:`DOWN` — anything else that did not reach the model: DNS, no route,
  a proxy's 502. An outage — never a crash.

Every state carries the HTTP status it came from (``state.code``).
"""
from __future__ import annotations

OK = "ok"
INCONCLUSIVE = "inconclusive"
ANSWERED = INCONCLUSIVE          # the old name
BUSY = "busy"
REFUSED = "refused"
DOWN = "down"

#: A 4xx with this text is a router / server mid-load: wait for it.
_BUSY_TEXT = ("loading", "pulling", "swapping", "unavailable",
              "starting", "not loaded", "not ready", "no model", "warming")
#: A 401/403 with this text is really an auth failure.
_AUTH_TEXT = ("auth", "api key", "api-key", "apikey", "x-api-key", "token",
              "credential", "forbidden", "permission", "unauthorized",
              "not allowed", "access denied")


class State(str):
    """A probe state that remembers its HTTP status (0 = none)."""

    code: int = 0

    def __new__(cls, value: str, code: int = 0) -> "State":
        obj = super().__new__(cls, value)
        obj.code = int(code or 0)
        return obj


def status_state(code: int, text: str = "") -> State:
    t = (text or "").lower()
    if 200 <= code < 300:
        return State(OK, code)
    if code == 502:
        return State(DOWN, code)
    if code >= 500 or code in (408, 429):
        return State(BUSY, code)
    if any(m in t for m in _BUSY_TEXT):
        return State(BUSY, code)
    if "not found" in t and "model" in t:
        # a router swapping or pulling THIS model; a bare route 404 (no
        # chat endpoint) is not busy — waiting on it would never end
        return State(BUSY, code)
    return State(INCONCLUSIVE, code)


def _links(exc: BaseException) -> list:
    out, todo = [], [exc]
    while todo and len(out) < 8:
        e = todo.pop(0)
        if not isinstance(e, BaseException) or e in out:
            continue
        out.append(e)
        todo += [getattr(e, "reason", None), e.__cause__, e.__context__]
    return out


def exc_state(exc: BaseException) -> State:
    """A transport failure (or a provider SDK's exception) as a state."""
    links = _links(exc)
    names = " ".join(type(x).__name__.lower() for x in links)
    text = " ".join(str(x).lower() for x in links)
    code = getattr(exc, "status_code", None)
    answered = "connection" not in names or getattr(exc, "response", None) is not None
    if isinstance(code, int) and 100 <= code < 600 and answered:
        # an HTTP answer came back (a proxy's 503 whose body says
        # "connection refused" is the proxy talking, not a refused socket).
        # litellm's APIConnectionError carries a status_code with no
        # response: nothing answered, so it is read as a socket failure.
        return status_state(code, text)
    if any(isinstance(x, TimeoutError) for x in links) \
            or "timeout" in names or "timed out" in text:
        return State(BUSY)
    if any(isinstance(x, (ConnectionRefusedError, ConnectionResetError))
           for x in links) or "connection refused" in text \
            or "connection reset" in text:
        return State(REFUSED)
    if "connection" in names or any(isinstance(x, OSError) for x in links):
        return State(DOWN)        # DNS, no route, TLS: never reached it
    return State(DOWN)


def is_auth(code: int, text: str) -> bool:
    return code in (401, 403) and any(m in (text or "").lower()
                                      for m in _AUTH_TEXT)


def same_client_error(probe_code: int, request_codes: set) -> bool:
    """The probe and the request were refused alike: both 401/403, or both
    the same other 4xx — a clear config problem, not the model's state."""
    if not probe_code or not 400 <= probe_code < 500:
        return False
    if probe_code in (401, 403):
        return bool(request_codes & {401, 403})
    return probe_code in request_codes


__all__ = ["ANSWERED", "BUSY", "DOWN", "INCONCLUSIVE", "OK", "REFUSED",
           "State", "exc_state", "is_auth", "same_client_error",
           "status_state"]
