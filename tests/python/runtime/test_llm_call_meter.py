"""Requests actually sent to the LLM, counted and attributed.

"One question, forty calls" is the complaint; the meter is the answer. It
counts at the WIRE (one per HTTP attempt, retries included) and attributes each
call to the chat session that caused it.
"""
import threading

import pytest

from aiforge_core.llm import call_meter
from aiforge_core.runtime import request_context


@pytest.fixture(autouse=True)
def _clean():
    call_meter.reset_all()
    yield
    call_meter.reset_all()


def test_counts_per_turn_session_and_total():
    call_meter.turn_reset(5)
    for _ in range(3):
        call_meter.record("doer", session_id=5)
    snap = call_meter.snapshot(5)
    assert snap["turn"] == 3 and snap["session"] == 3 and snap["total"] == 3
    assert snap["by_role"] == {"doer": 3}

    call_meter.turn_reset(5)                     # next turn
    call_meter.record("doer", session_id=5)
    snap = call_meter.snapshot(5)
    assert snap["turn"] == 1                     # per-turn resets…
    assert snap["session"] == 4                  # …the chat total does not


def test_unattributed_calls_still_count_machine_wide(monkeypatch):
    monkeypatch.delenv("AIFORGE_CURRENT_SESSION", raising=False)
    call_meter.record("learner")                 # a fold, no session bound
    assert call_meter.snapshot(None)["total"] == 1
    assert call_meter.snapshot(7)["session"] == 0


def test_session_attribution_comes_from_the_request_context():
    token = request_context.set_session_id(42)
    try:
        call_meter.record("doer")
    finally:
        request_context.reset_session_id(token)
    assert call_meter.snapshot(42)["session"] == 1


def test_the_process_global_env_session_never_bills_a_chat(monkeypatch):
    """AIFORGE_CURRENT_SESSION is set by the chat route and never cleared, so
    every bare thread in the process reads whichever chat ran last. A fold's
    calls billed to an innocent chat is worse than an unattributed call."""
    monkeypatch.setenv("AIFORGE_CURRENT_SESSION", "111")
    call_meter.record("learner")                   # e.g. a background fold
    assert call_meter.snapshot(111)["session"] == 0
    assert call_meter.snapshot(None)["total"] == 1


def test_a_cancelled_turns_zombie_retries_do_not_bill_the_next_turn():
    """A cancelled generation is ABANDONED: its thread keeps retrying with the
    old turn's context bound. Those calls belong to the turn that made them."""
    sid = 777
    tok = call_meter.turn_reset(sid)
    cv = call_meter.bind_turn(tok)
    call_meter.record("doer", session_id=sid)      # the turn's own call
    assert call_meter.snapshot(sid)["turn"] == 1

    # User presses Stop and sends a new message; the zombie is still retrying
    # inside the OLD context (still stamped with the old epoch).
    call_meter.turn_reset(sid)
    call_meter.record("doer", session_id=sid)      # zombie, old epoch via cv
    call_meter.reset_turn(cv)
    snap = call_meter.snapshot(sid)
    assert snap["turn"] == 0                       # not billed to the new turn
    assert snap["session"] == 2                    # but still a real request


def test_per_minute_window_drops_old_calls(monkeypatch):
    call_meter.record("doer", session_id=1, now=1000.0)
    call_meter.record("doer", session_id=1, now=1001.0)
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1002.0)
    assert call_meter.snapshot(1)["per_minute"] == 2
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1120.0)   # 2 min on
    assert call_meter.snapshot(1)["per_minute"] == 0
    assert call_meter.snapshot(1)["session"] == 2      # totals are not a window


def test_session_table_is_bounded():
    for sid in range(call_meter._MAX_SESSIONS + 50):
        call_meter.record("doer", session_id=sid)
    assert len(call_meter._sessions) == call_meter._MAX_SESSIONS
    assert call_meter.snapshot(0)["session"] == 0      # oldest evicted
    assert call_meter.snapshot(call_meter._MAX_SESSIONS + 49)["session"] == 1


def test_record_never_raises_on_a_broken_context(monkeypatch):
    monkeypatch.setattr(request_context, "get_session_id",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    call_meter.record("doer")                          # must not propagate
    assert call_meter.snapshot(None)["total"] == 1


def test_concurrent_records_do_not_lose_counts():
    def bump():
        for _ in range(200):
            call_meter.record("doer", session_id=9)
    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert call_meter.snapshot(9)["session"] == 1600


def test_every_http_attempt_is_counted(monkeypatch):
    """Retries are real requests — a provider counts them, so we do too."""
    from aiforge_core.llm.client import _http

    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise TimeoutError("transient")

    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "3")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BASE_S", "0")
    # Count through the REAL _post so the retries are metered, not just made.
    monkeypatch.setattr(_http._rl, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(_http, "_preflight", lambda *_a: None)
    monkeypatch.setattr(_http, "_estimate_tokens", lambda *_a: 1)
    monkeypatch.setattr(_http.urllib.request, "urlopen", lambda *a, **k: boom())
    monkeypatch.setattr(_http.time, "sleep", lambda *_a: None)
    from aiforge_core.llm.types import Endpoint
    ep = Endpoint(base_url="http://127.0.0.1:1", api_key="", model="m",
                  provider="x", role="doer", extras={})
    with pytest.raises(TimeoutError):
        _http._post_with_retry(ep, b"{}", 1, role="doer", source="test")
    assert calls["n"] == 3                          # three attempts made
    assert call_meter.snapshot(None)["total"] == 3  # …and three counted


def test_post_records_before_the_request_goes_out(monkeypatch):
    """The counter sits in _post itself, so every path through the client —
    retry, fallback, escalation, native tool-calling — is counted once."""
    from aiforge_core.llm.client import _http

    from aiforge_core.llm.types import Endpoint
    ep = Endpoint(base_url="http://127.0.0.1:1", api_key="", model="m",
                  provider="x", role="doer", extras={})

    monkeypatch.setattr(_http._rl, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(_http, "_preflight", lambda *_a: None)
    monkeypatch.setattr(_http, "_estimate_tokens", lambda *_a: 1)
    monkeypatch.setattr(_http.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no server")))
    with pytest.raises(OSError):
        _http._post(ep, b"{}", 1)
    assert call_meter.snapshot(None)["total"] == 1


def test_an_attempt_that_never_reaches_the_network_is_not_counted(monkeypatch):
    """A sleeping local box fails the preflight before a byte leaves. Counting
    those would let the meter manufacture the overload it exists to diagnose."""
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint
    ep = Endpoint(base_url="http://127.0.0.1:1", api_key="", model="m",
                  provider="x", role="doer", extras={})
    monkeypatch.setattr(_http._rl, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(_http, "_estimate_tokens", lambda *_a: 1)
    monkeypatch.setattr(_http, "_preflight",
                        lambda *_a: (_ for _ in ()).throw(ConnectionError("asleep")))
    with pytest.raises(ConnectionError):
        _http._post(ep, b"{}", 1)
    assert call_meter.snapshot(None)["total"] == 0


def test_chat_turn_publishes_the_request_count(tmp_path):
    """The loop's usage event carries the counts the badge renders. The turn
    BOUNDARY belongs to the route, so the loop must not reset it — a team-mode
    turn never enters the loop at all, and the pre-loop enhancer calls are part
    of the same message."""
    from aiforge_core.runtime import chat_agent as ca

    sid = 4242
    call_meter.turn_reset(sid)                     # the route's boundary
    call_meter.record("doer", session_id=sid)      # a pre-loop enhancer call
    seq = ['ACTION: list_dir\nARGS_JSON: {"path": "."}', "FINAL: done"]

    def fn(role, messages, **kw):
        call_meter.record("doer", session_id=sid)  # this turn's model call
        return seq.pop(0)

    evs = list(ca.run_chat_agent([{"role": "user", "content": "hi"}],
                                 cwd=str(tmp_path), complete_fn=fn,
                                 session_id=sid))
    usage = [e for e in evs if e["type"] == "usage"]
    assert usage, "no usage event emitted"
    last = usage[-1]
    assert "llm_turn" in last and "llm_session" in last and "llm_per_min" in last
    # The pre-loop call is still part of THIS turn — the loop did not erase it.
    assert last["llm_turn"] >= 2
    assert call_meter.snapshot(sid)["turn"] == call_meter.snapshot(sid)["session"]


def test_role_is_attributed_so_by_role_is_not_dead(tmp_path):
    """"Which agent is burning the calls" was a permanently empty dict:
    request_context.set_role had no caller anywhere."""
    from aiforge_core.runtime.chat_agent._context import _generation

    def complete_fn(role, convo):
        call_meter.record()                        # role from the context
        return "FINAL: ok"

    token = request_context.set_session_id(515)
    try:
        _generation._complete_cancellable(complete_fn, "learner", [], 515)
    finally:
        request_context.reset_session_id(token)
    assert call_meter.snapshot(515)["by_role"] == {"learner": 1}


def test_generation_thread_inherits_the_session_context(tmp_path):
    """The LLM client runs on a side thread, and a fresh thread starts with an
    EMPTY context — so without explicit propagation every chat request would be
    attributed to nobody."""
    from aiforge_core.runtime.chat_agent._context import _generation

    seen = {}

    def complete_fn(role, convo):
        seen["sid"] = request_context.get_session_id()
        return "FINAL: ok"

    token = request_context.set_session_id(31337)
    try:
        out = _generation._complete_cancellable(complete_fn, "doer", [], 31337)
    finally:
        request_context.reset_session_id(token)
    assert out == "FINAL: ok"
    assert seen["sid"] == "31337"
