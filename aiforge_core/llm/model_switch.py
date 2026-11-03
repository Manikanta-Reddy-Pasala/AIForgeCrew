"""The user changed a chat's model while its turn is running.

The picker used to be locked for the length of a run, so a chat stuck on a
model that had stopped answering could only be Stopped (losing the step) and
started again. Now the new pick is saved as the chat's model as usual
(config/chat_models) and recorded here; the request that is waiting on the old
model is aborted and the SAME step is sent again on the new one — the turn
goes on, its history intact. With nothing in flight the next step simply
resolves the new pick.

One record per chat: ``{"seq", "from", "to", "from_url", "to_url"}``. ``seq`` only grows, so every
request knows whether a switch happened after it started (and is therefore
waiting on the wrong model) without anyone having to consume the record — two
requests of one turn in flight at once are both retried. The transport side
lives in llm/model_failover (the per-request guard) and the client's
cancellable POST (which closes the connection the moment ``abort`` says so).
"""
from __future__ import annotations

import itertools
import threading

_LOCK = threading.Lock()
_SEQ = itertools.count(1)
_SWITCH: dict = {}       # session id -> {"seq", "from", "to"}
_INFLIGHT: dict = {}     # session id -> requests in flight that follow its pick


class ModelSwitched(Exception):
    """The in-flight request was aborted because the user picked another model
    for the chat. Never a failure of the model: the guard sends the same
    request again on the new pick (llm/model_failover)."""

    def __init__(self, to_model: str = "") -> None:
        self.to_model = to_model
        super().__init__(f"model switched to {to_model or 'another model'} by the user")


def _key(session_id) -> str:
    return str(session_id)


def norm_url(url) -> str:
    """Comparable endpoint: no trailing slash or ``/v1``, any case."""
    u = str(url or "").strip().rstrip("/").lower()
    return u[:-3] if u.endswith("/v1") else u


def same_endpoint(model: str, url, other_model: str, other_url) -> bool:
    """Is (model, url) the same served model as (other_model, other_url)? The
    same id on ANOTHER server is a different model to pick (a second box). An
    empty url means "the global endpoint, whatever it is": then only the ids
    are compared, never a guess about which host that is."""
    if str(model or "").strip() != str(other_model or "").strip():
        return False
    a, b = norm_url(url), norm_url(other_url)
    return not a or not b or a == b


def request(session_id, from_model: str, to_model: str,
            from_url: str = "", to_url: str = "") -> str:
    """Record that the chat now runs on ``to_model`` (at ``to_url``). Returns
    ``"retrying"`` when a request of the chat is in flight (it is aborted and
    retried on the new model), ``"next_step"`` when none is, or ``""`` when
    nothing changes."""
    to_model = str(to_model or "").strip()
    from_model = str(from_model or "").strip()
    if session_id in (None, "") or not to_model:
        return ""
    if to_model == from_model and norm_url(to_url) == norm_url(from_url):
        return ""
    with _LOCK:
        _SWITCH[_key(session_id)] = {
            "seq": next(_SEQ), "from": from_model, "to": to_model,
            "from_url": str(from_url or ""), "to_url": str(to_url or "")}
        busy = _INFLIGHT.get(_key(session_id), 0) > 0
    return "retrying" if busy else "next_step"


def latest(session_id) -> "dict | None":
    """The chat's latest switch (a copy), or None."""
    with _LOCK:
        sw = _SWITCH.get(_key(session_id))
        return dict(sw) if sw else None


def seq_now(session_id) -> int:
    """The latest switch's sequence number (0 = never switched)."""
    sw = latest(session_id)
    return int(sw["seq"]) if sw else 0


def enter(session_id) -> None:
    """A request that follows the chat's pick is in flight."""
    with _LOCK:
        k = _key(session_id)
        _INFLIGHT[k] = _INFLIGHT.get(k, 0) + 1


def leave(session_id) -> None:
    with _LOCK:
        k = _key(session_id)
        n = _INFLIGHT.get(k, 0) - 1
        if n > 0:
            _INFLIGHT[k] = n
        else:
            _INFLIGHT.pop(k, None)


def in_flight(session_id) -> int:
    with _LOCK:
        return _INFLIGHT.get(_key(session_id), 0)


def forget(session_id=None) -> None:
    """Drop a chat's record (deleted chat), or every record."""
    with _LOCK:
        if session_id is None:
            _SWITCH.clear()
            _INFLIGHT.clear()
        else:
            _SWITCH.pop(_key(session_id), None)


__all__ = ["ModelSwitched", "enter", "forget", "in_flight", "latest", "leave",
           "norm_url", "request", "same_endpoint", "seq_now"]
