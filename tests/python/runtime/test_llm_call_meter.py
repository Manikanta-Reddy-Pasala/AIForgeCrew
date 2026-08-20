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
        # A REFUSED connection, not a read timeout: nothing reached the server,
        # so this is the failure class that still gets its retries.
        raise ConnectionRefusedError("transient")

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
    with pytest.raises(ConnectionRefusedError):
        _http._post_with_retry(ep, b"{}", 1, role="doer", source="test")
    assert calls["n"] == 3                          # three attempts made
    assert call_meter.snapshot(None)["total"] == 3  # …and three counted


def _endpoint():
    from aiforge_core.llm.types import Endpoint
    return Endpoint(base_url="http://127.0.0.1:1", api_key="", model="m",
                    provider="x", role="doer", extras={})


def _timeout_post(calls, *, shipped: bool):
    """A _post that times out, either AFTER the prompt reached the server
    (shipped) or BEFORE it ever did (the rate limiter giving up, a stalled
    TLS handshake) — the distinction the retry rule turns on."""
    def fn(_ep, _payload, _timeout, *, role=None, sent=None, max_wait_s=None):
        calls["n"] += 1
        if shipped and sent is not None:
            sent[0] = True
        raise TimeoutError("The read operation timed out")
    return fn


def test_read_timeout_is_not_re_posted(monkeypatch):
    """The server already HAS the prompt — a retry adds a second generation to
    a box that could not finish the first one in time."""
    from aiforge_core.llm.client import _http

    calls = {"n": 0}
    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "3")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BASE_S", "0")
    monkeypatch.delenv("AIFORGE_LLM_RETRY_TIMEOUT_MAX", raising=False)
    monkeypatch.setattr(_http, "_post", _timeout_post(calls, shipped=True))
    monkeypatch.setattr(_http.time, "sleep", lambda *_a: None)
    with pytest.raises(TimeoutError):
        _http._post_with_retry(_endpoint(), b"{}", 20, role="triage",
                               source="test")
    assert calls["n"] == 1


def test_a_timeout_that_never_reached_the_server_still_retries(monkeypatch):
    """The rate limiter gives up with a TimeoutError, and so does a stalled
    connect/TLS handshake — neither cost the server anything, and the bucket
    refills DURING the backoff, so this is the class where a retry is most
    likely to work. Treating it as "the server already has this" silently
    killed every call during a rate-limit burst."""
    from aiforge_core.llm.client import _http

    calls = {"n": 0}
    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "3")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BASE_S", "0")
    monkeypatch.setattr(_http, "_post", _timeout_post(calls, shipped=False))
    monkeypatch.setattr(_http.time, "sleep", lambda *_a: None)
    with pytest.raises(TimeoutError):
        _http._post_with_retry(_endpoint(), b"{}", 20, role="triage",
                               source="test")
    assert calls["n"] == 3


def test_timeout_retries_can_be_turned_back_on(monkeypatch):
    """The no-re-POST rule is a default, not a law: an operator who wants the
    old behaviour (or none of the limits at all) sets the knob."""
    from aiforge_core.llm.client import _http

    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise TimeoutError("The read operation timed out")

    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "3")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BASE_S", "0")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_TIMEOUT_MAX", "3")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BUDGET", "0")   # no deadline either
    monkeypatch.setattr(_http, "_post", lambda *a, **k: boom())
    monkeypatch.setattr(_http.time, "sleep", lambda *_a: None)
    with pytest.raises(TimeoutError):
        _http._post_with_retry(_endpoint(), b"{}", 20, role="triage",
                               source="test")
    assert calls["n"] == 3


def test_retry_budget_stops_a_slow_chain(monkeypatch):
    """Retries live inside the CALLER's deadline. Three slow 502s must not
    turn a 20s call into a minute — and the retry that does not fit is not
    made as a STUB either: a 3-second attempt at a 900-second generation is
    the abandoned request this whole rule exists to prevent."""
    from aiforge_core.llm.client import _http

    clock = {"t": 1000.0}
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        clock["t"] += 19.0          # each attempt burns nearly the timeout
        raise OSError("slow 502")

    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "3")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BASE_S", "0.5")
    monkeypatch.setattr(_http, "_post", lambda *a, **k: boom())
    monkeypatch.setattr(_http.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(_http.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + s))
    with pytest.raises(OSError):
        _http._post_with_retry(_endpoint(), b"{}", 20, role="triage",
                               source="test")
    # budget = max(20*1.5, 20+10) = 30s. Attempt 2 would start at ~19.5s and
    # needs the FULL 20s, which does not fit — so it is never made.
    assert calls["n"] == 1


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


def test_windows_are_rolling_not_cumulative():
    """A call ages OUT of a window. The hour count is what happened in the
    last hour, not everything since boot — that is what `total` is for."""
    from aiforge_core.llm import call_meter
    call_meter.reset_all()
    now = 100_020.0                                # a whole-minute boundary
    call_meter.record("learner", now=now - 3000)   # 50 min ago
    call_meter.record("learner", now=now - 700)    # ~12 min ago
    call_meter.record("doer", now=now - 10)        # this minute

    # Freeze the clock so the windows are computed against a known `now`.
    import time as _time
    real = _time.monotonic
    _time.monotonic = lambda: now
    try:
        snap = call_meter.global_snapshot()
    finally:
        _time.monotonic = real

    assert snap["per_minute"] == 1
    assert snap["last_15m"] == 2
    assert snap["last_60m"] == 3
    assert snap["total"] == 3
    assert snap["by_role"] == {"learner": 2, "doer": 1}
    call_meter.reset_all()


def test_entries_older_than_the_hour_are_dropped():
    """The buffer must not grow forever — anything past the widest window is
    trimmed on write."""
    from aiforge_core.llm import call_meter
    call_meter.reset_all()
    now = 200_000.0
    for i in range(5):
        call_meter.record("learner", now=now - 7200 + i)   # 2 hours ago
    call_meter.record("doer", now=now)
    assert len(call_meter._recent) == 1
    assert call_meter._total == 6          # the TOTAL still counts them
    call_meter.reset_all()


def test_series_buckets_by_minute_oldest_first():
    """One slot per WALL minute, oldest first. Minute-aligned on purpose: it
    is what makes a read O(60) instead of a scan of the hour under the lock."""
    from aiforge_core.llm import call_meter
    call_meter.reset_all()
    now = 300_000.0                       # 300000 // 60 == 5000 exactly
    call_meter.record("doer", now=now - 95)          # minute 4998
    call_meter.record("doer", now=now - 90)          # minute 4998
    call_meter.record("doer", now=now - 30)          # minute 4999
    call_meter.record("doer", now=now)               # minute 5000

    import time as _time
    real = _time.monotonic
    _time.monotonic = lambda: now
    try:
        series = call_meter.global_snapshot()["series_60m"]
    finally:
        _time.monotonic = real

    assert len(series) == 60
    assert series[-1] == 1 and series[-2] == 1 and series[-3] == 2
    assert sum(series) == 4
    call_meter.reset_all()


def test_reads_do_not_scan_the_hour():
    """The lock these reads take is the one every LLM call takes just before
    it POSTs. A full scan of an hour of calls there stalls the hot path
    exactly when the system is busiest — which is when someone opens the
    meter. Buckets keep a read at ~60 slots however many calls landed."""
    from aiforge_core.llm import call_meter
    call_meter.reset_all()
    base = 500_000.0
    for i in range(5000):
        call_meter.record("doer", now=base + i * 0.5)   # 5000 calls / ~42 min
    now = base + 2500
    import time as _time
    real = _time.monotonic
    _time.monotonic = lambda: now
    try:
        snap = call_meter.global_snapshot()
    finally:
        _time.monotonic = real
    # At most one slot per minute of history, whatever the call volume.
    assert len(call_meter._buckets) <= call_meter._BUCKETS + 1
    assert snap["total"] == 5000
    assert snap["last_60m"] == 5000
    call_meter.reset_all()


def test_rate_capped_is_about_the_last_minute_only():
    """It drives a UI warning that the numbers are a floor, so it must mean
    "data is missing FROM THE RATE, right now" — not "a buffer once filled",
    which stuck on for the life of the process and mislabelled four exact
    numbers as estimates."""
    from aiforge_core.llm import call_meter
    call_meter.reset_all()
    now = 700_040.0
    for i in range(50):
        call_meter.record("doer", now=now + i * 0.01)
    import time as _time
    real = _time.monotonic
    _time.monotonic = lambda: now + 1
    try:
        assert call_meter.global_snapshot()["rate_capped"] is False
    finally:
        _time.monotonic = real

    # A drop two minutes ago says nothing about the current rate.
    call_meter._dropped_at = now - 120
    _time.monotonic = lambda: now
    try:
        assert call_meter.global_snapshot()["rate_capped"] is False
        call_meter._dropped_at = now - 5
        assert call_meter.global_snapshot()["rate_capped"] is True
    finally:
        _time.monotonic = real
    call_meter.reset_all()


def test_breakdown_labels_are_bounded():
    """Model ids are operator-editable and mlx-lm's are filesystem paths. One
    bad minute must not ship a six-figure JSON blob to every polling browser
    (assembled under the lock every LLM call takes) to render 8 rows."""
    from aiforge_core.llm import call_meter
    call_meter.reset_all()
    now = 900_060.0
    for i in range(500):
        call_meter.record("doer", model=f"/very/long/model/path/number/{i}" + "x" * 200,
                          now=now)
    import time as _time
    real = _time.monotonic
    _time.monotonic = lambda: now
    try:
        snap = call_meter.global_snapshot()
    finally:
        _time.monotonic = real
    assert len(snap["by_model"]) <= call_meter._LABELS_PER_BUCKET + 1
    assert max(len(k) for k in snap["by_model"]) <= call_meter._LABEL_MAX
    # Nothing is lost from the TOTALS — the overflow is folded into one row.
    assert sum(snap["by_model"].values()) == 500
    assert snap["last_60m"] == 500
    call_meter.reset_all()


def test_model_is_recorded_alongside_provider():
    """With one provider class serving every endpoint, "by provider" is a
    constant — the model is the axis that distinguishes anything."""
    from aiforge_core.llm import call_meter
    call_meter.reset_all()
    now = 800_040.0
    call_meter.record("doer", provider="openai_compatible",
                      model="qwen3-coder", now=now)
    call_meter.record("learner", provider="openai_compatible",
                      model="qwen3-4b", now=now)
    import time as _time
    real = _time.monotonic
    _time.monotonic = lambda: now
    try:
        snap = call_meter.global_snapshot()
    finally:
        _time.monotonic = real
    assert snap["by_model"] == {"qwen3-coder": 1, "qwen3-4b": 1}
    assert snap["by_provider"] == {"openai_compatible": 2}
    call_meter.reset_all()


def test_a_fast_failure_still_gets_every_retry(monkeypatch):
    """The budget must only bite calls that actually burned their deadline.
    A refused connection costs nothing, so the chain keeps its attempts."""
    from aiforge_core.llm.client import _http

    clock = {"t": 2000.0}
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        clock["t"] += 0.01
        raise ConnectionRefusedError("refused")

    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "3")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BASE_S", "0.5")
    monkeypatch.setattr(_http, "_post", lambda *a, **k: boom())
    monkeypatch.setattr(_http.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(_http.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + s))
    with pytest.raises(ConnectionRefusedError):
        _http._post_with_retry(_endpoint(), b"{}", 20, role="triage",
                               source="test")
    assert calls["n"] == 3


def test_a_shipped_timeout_is_marked_all_the_way_up(monkeypatch):
    """The transport declining to re-POST is worth nothing if the layer above
    re-issues the same completion five more times. complete() raises with the
    marker so the chat loop can tell "the box has this prompt" from "we never
    reached it"."""
    import pytest as _pytest
    from aiforge_core.llm import client as _client
    from aiforge_core.llm.client import _http

    calls = {"n": 0}
    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "1")
    monkeypatch.setattr(_http, "_post", _timeout_post(calls, shipped=True))
    monkeypatch.setattr(_http.time, "sleep", lambda *_a: None)
    with _pytest.raises(RuntimeError) as ei:
        _client.complete("triage", [{"role": "user", "content": "hi"}],
                         timeout_s=5)
    assert "llm.exhausted" in str(ei.value)
    assert _client.shipped_timeout(ei.value) is True


def test_a_timeout_we_never_sent_is_not_marked(monkeypatch):
    """The rate limiter giving up must stay retryable at every layer."""
    import pytest as _pytest
    from aiforge_core.llm import client as _client
    from aiforge_core.llm.client import _http

    calls = {"n": 0}
    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "1")
    monkeypatch.setattr(_http, "_post", _timeout_post(calls, shipped=False))
    monkeypatch.setattr(_http.time, "sleep", lambda *_a: None)
    with _pytest.raises(RuntimeError) as ei:
        _client.complete("triage", [{"role": "user", "content": "hi"}],
                         timeout_s=5)
    assert _client.shipped_timeout(ei.value) is False


def test_an_unreachable_endpoint_counts_zero_requests(monkeypatch):
    """The preflight proves nothing can be sent. Counting there manufactured
    "18 requests · 18/min" against a sleeping box — on the chat path only,
    because it preflighted in a different place. One preflight, one rule."""
    from aiforge_core.llm import call_meter
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint

    call_meter.reset_all()
    ep = Endpoint(base_url="http://127.0.0.1:1", api_key="", model="m",
                  provider="openai_compatible", role="doer", extras={})
    import threading
    for cancel in (None, threading.Event()):     # urllib path, then chat path
        _http._CANCEL.set(cancel)
        try:
            _http._post(ep, b"{}", 1, role="doer")
        except Exception:  # noqa: BLE001 — unreachable, as intended
            pass
    _http._CANCEL.set(None)
    assert call_meter.global_snapshot(series=False)["total"] == 0
    call_meter.reset_all()
