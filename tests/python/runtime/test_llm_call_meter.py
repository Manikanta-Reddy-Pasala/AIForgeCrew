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
    assert snap["turn"] == 3
    assert snap["session"] == 3
    assert snap["total"] == 3
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
    def fn(_ep, _payload, _timeout, *, role=None, sent=None, max_wait_s=None,
           throttled=None, **_kw):
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
    ep = _endpoint()
    with pytest.raises(TimeoutError):
        _http._post_with_retry(ep, b"{}", 20, role="triage",
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
    ep = _endpoint()
    with pytest.raises(TimeoutError):
        _http._post_with_retry(ep, b"{}", 20, role="triage",
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
    ep = _endpoint()
    with pytest.raises(TimeoutError):
        _http._post_with_retry(ep, b"{}", 20, role="triage",
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
    ep = _endpoint()
    with pytest.raises(OSError):
        _http._post_with_retry(ep, b"{}", 20, role="triage",
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
    assert "llm_turn" in last
    assert "llm_session" in last
    assert "llm_per_min" in last
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
    assert series[-1] == 1
    assert series[-2] == 1
    assert series[-3] == 2
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
    ep = _endpoint()
    with pytest.raises(ConnectionRefusedError):
        _http._post_with_retry(ep, b"{}", 20, role="triage",
                               source="test")
    assert calls["n"] == 3


def test_a_shipped_timeout_is_marked_all_the_way_up(monkeypatch):
    """The transport declining to re-POST is worth nothing if the layer above
    re-issues the same completion five more times. complete() raises with the
    marker so the chat loop can tell "the box has this prompt" from "we never
    reached it"."""
    import pytest
    from aiforge_core.llm import client as _client
    from aiforge_core.llm.client import _http

    calls = {"n": 0}
    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "1")
    monkeypatch.setattr(_http, "_post", _timeout_post(calls, shipped=True))
    monkeypatch.setattr(_http.time, "sleep", lambda *_a: None)
    with pytest.raises(RuntimeError) as ei:
        _client.complete("triage", [{"role": "user", "content": "hi"}],
                         timeout_s=5)
    assert "llm.exhausted" in str(ei.value)
    assert _client.shipped_timeout(ei.value) is True


def test_a_timeout_we_never_sent_is_not_marked(monkeypatch):
    """The rate limiter giving up must stay retryable at every layer."""
    import pytest
    from aiforge_core.llm import client as _client
    from aiforge_core.llm.client import _http

    calls = {"n": 0}
    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "1")
    monkeypatch.setattr(_http, "_post", _timeout_post(calls, shipped=False))
    monkeypatch.setattr(_http.time, "sleep", lambda *_a: None)
    with pytest.raises(RuntimeError) as ei:
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


# ─────────────────────── failed requests ────────────────────────────────
# A failed request is still a request: the provider counted it and rate-limited
# on it, and the retry storm it belongs to is the thing the meter exists to
# expose. So failures are counted ALONGSIDE the rate, never subtracted from it.


def test_failures_are_counted_beside_the_rate_not_out_of_it(monkeypatch):
    toks = [call_meter.record("doer", session_id=3, now=1000.0)
            for _ in range(3)]
    call_meter.record_failure(toks[0], "http_500", now=1000.1)
    call_meter.record_failure(toks[1], "timeout", now=1000.2)
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1001.0)

    snap = call_meter.snapshot(3)
    assert snap["per_minute"] == 3            # every attempt went out…
    assert snap["failed_per_minute"] == 2     # …two of them answered nothing
    assert snap["turn_failed"] == 2
    assert snap["session_failed"] == 2
    assert snap["failed"] == 2
    assert snap["total"] == 3

    g = call_meter.global_snapshot()
    assert g["per_minute"] == 3
    assert g["failed_per_minute"] == 2
    assert g["last_60m"] == 3
    assert g["failed_60m"] == 2
    assert g["by_fail_reason"] == {"http_500": 1, "timeout": 1}
    assert len(g["series_fail_60m"]) == len(g["series_60m"]) == 60
    assert sum(g["series_fail_60m"]) == 2


def test_a_failure_is_billed_to_the_minute_of_its_send(monkeypatch):
    """A 600s read timeout that gives up now was traffic ten minutes ago.
    Charging it to the current minute invents a burst that never happened and
    leaves the minute it belongs to looking clean."""
    tok = call_meter.record("doer", now=1000.0)
    call_meter.record_failure(tok, "timeout", now=1000.0 + 600)
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1000.0 + 601)

    g = call_meter.global_snapshot()
    # 10 minutes on, neither the send nor its failure is in the last minute…
    assert g["per_minute"] == 0
    assert g["failed_per_minute"] == 0
    # …and inside the hour they sit in the SAME minute slot.
    assert g["last_60m"] == 1
    assert g["failed_60m"] == 1
    i = [n for n, v in enumerate(g["series_60m"]) if v]
    assert i
    assert [n for n, v in enumerate(g["series_fail_60m"]) if v] == i


def test_a_failure_older_than_the_history_counts_but_reports_no_window(monkeypatch):
    """Beyond the reported hour there is no send-minute left to charge. It
    counts in the lifetime total — it happened — and in NO window, because the
    send it belongs to is outside those windows too. Re-stamping it to the
    current minute would report a burst that never happened and, worse, put
    `failed_60m` above `last_60m`: failures are a subset of requests, and the
    window numbers have to hold that too."""
    tok = call_meter.record("doer", now=1000.0)
    late = 1000.0 + call_meter._RETAIN_S + 300
    call_meter.record_failure(tok, "timeout", now=late)
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: late + 1)

    g = call_meter.global_snapshot()
    assert g["failed"] == 1                       # lifetime: it happened
    assert g["failed_per_minute"] == 0
    assert g["failed_60m"] == 0
    assert g["last_60m"] == 0
    assert sum(g["series_fail_60m"]) == 0


def test_the_window_cutoff_matches_the_window_the_meter_reports(monkeypatch):
    """`_RETAIN_S` is 60 minutes but the windows sum 59 whole minutes back, so
    a cutoff written against retention drops a 59-minute-old failure into a
    bucket that exists and that nothing reports: `failed` up, every window
    flat."""
    tok = call_meter.record("doer", now=1000.0)
    call_meter.record_failure(tok, "timeout", now=1000.0 + 3599)
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1000.0 + 3600)
    g = call_meter.global_snapshot()
    # Either it is inside the reported hour or it is not — but the failure and
    # its send must agree, and no failure may sit in a limbo bucket.
    assert g["failed_60m"] == g["last_60m"]
    assert sum(g["series_fail_60m"]) == g["failed_60m"]


def test_failures_never_outnumber_sends_in_any_window(monkeypatch):
    """The invariant the UI prints ("N failed" under a window's own count):
    for every reported window, failures <= requests."""
    toks = [call_meter.record("doer", now=1000.0 + i) for i in range(5)]
    for i, t in enumerate(toks):
        call_meter.record_failure(t, "timeout", now=1000.0 + 4000 + i)
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1000.0 + 4010)
    g = call_meter.global_snapshot()
    assert g["failed_per_minute"] <= g["per_minute"]
    assert g["failed_15m"] <= g["last_15m"]
    assert g["failed_60m"] <= g["last_60m"]
    assert all(f <= n for f, n in zip(g["series_fail_60m"], g["series_60m"]))


def test_out_of_order_failures_do_not_freeze_the_minute_window(monkeypatch):
    """Failures arrive in the order calls GIVE UP, not the order they were
    sent, so an older stamp lands after a newer one. In a popleft-trimmed deque
    that parks a stale entry at the head and the window never empties again."""
    old_tok = call_meter.record("doer", now=1000.0)
    new_tok = call_meter.record("doer", now=1030.0)
    call_meter.record_failure(new_tok, "http_500", now=1031.0)   # newer FIRST
    call_meter.record_failure(old_tok, "timeout", now=1032.0)

    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1035.0)
    assert call_meter.global_snapshot()["failed_per_minute"] == 2
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1075.0)
    # 1000.0 has aged out of the 60s window; 1030.0 has not.
    assert call_meter.global_snapshot()["failed_per_minute"] == 1
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1200.0)
    assert call_meter.global_snapshot()["failed_per_minute"] == 0
    assert not call_meter._recent_fail


def test_turn_failures_reset_with_the_turn_and_zombies_do_not_land():
    tok = call_meter.turn_reset(21)
    cv = call_meter.bind_turn(tok)
    t = call_meter.record("doer", session_id=21)
    call_meter.record_failure(t, "http_500")
    assert call_meter.snapshot(21)["turn_failed"] == 1

    call_meter.turn_reset(21)                       # the user sent a new message
    assert call_meter.snapshot(21)["turn_failed"] == 0
    # The abandoned generation's retry finally fails, still stamped with the
    # OLD turn: it belongs to the message that made it, not to this one.
    zombie = call_meter.record("doer", session_id=21)
    call_meter.record_failure(zombie, "timeout")
    call_meter.reset_turn(cv)
    snap = call_meter.snapshot(21)
    assert snap["turn_failed"] == 0
    assert snap["session_failed"] == 2              # the chat still paid for it


def test_a_failure_never_mints_a_session(monkeypatch):
    """The session was evicted (or the API restarted) between send and
    failure. Charge the machine, never mint a slot — minting one evicts a live
    chat's counters to make room for a dead one."""
    monkeypatch.delenv("AIFORGE_CURRENT_SESSION", raising=False)
    tok = (call_meter.time.monotonic(), "999", 0)   # a token record() would give
    call_meter.record_failure(tok, "timeout")
    assert "999" not in call_meter._sessions
    assert call_meter.global_snapshot()["failed"] == 1


def test_a_failure_with_no_token_is_not_counted():
    """`record` returns None ONLY when it could not count the send. Counting
    the failure anyway put `failed` above `total` — a meter reporting traffic
    the process never sent, and a sparkline minute with no bar to colour."""
    for junk in (None, "nope", (1, 2), object(), (1, 2, 3, 4), 0, ""):
        call_meter.record_failure(junk, "timeout")
    g = call_meter.global_snapshot(series=False)
    assert g["failed"] == 0
    assert g["total"] == 0


def test_failures_never_exceed_requests_on_the_wire_path(monkeypatch):
    """The invariant end to end: whatever `record` could not count, the
    failure path must not count either."""
    from aiforge_core.llm.client import _http

    monkeypatch.setattr(_http, "_record_request", lambda *_a, **_k: None)
    _http._record_failure(None, RuntimeError("boom"))
    g = call_meter.global_snapshot(series=False)
    assert g["failed"] == 0
    assert g["total"] == 0


def test_a_failure_can_never_outnumber_its_turn(monkeypatch):
    """`turn_failed` must stay <= `turn` for the turn on screen. The failure
    of a request sent BEFORE the current message belongs to the message that
    sent it, whatever the meter knows about turns."""
    call_meter.turn_reset(31)
    tok = call_meter.record("doer", session_id=31)   # counted against turn 1
    call_meter.turn_reset(31)                        # new message
    call_meter.record_failure(tok, "timeout")
    snap = call_meter.snapshot(31)
    assert snap["turn"] == 0
    assert snap["turn_failed"] == 0
    assert snap["session_failed"] == 1


def test_fail_reason_labels_are_bounded():
    for i in range(call_meter._LABELS_PER_BUCKET + 25):
        call_meter.record_failure(None, f"reason_{i}" + "x" * 200)
    reasons = call_meter.global_snapshot()["by_fail_reason"]
    assert len(reasons) <= call_meter._LABELS_PER_BUCKET + 1   # +"…other"
    assert all(len(k) <= call_meter._LABEL_MAX for k in reasons)


def test_reset_all_clears_the_rate_capped_flag(monkeypatch):
    """`_dropped_at` was missing from reset_all's `global` line, so the
    assignment bound a local and one overflowing test left every later snapshot
    claiming the per-minute figure was a floor."""
    # setattr so the flag is restored even if an assertion below fails — a
    # leaked _dropped_at is exactly the cross-test bleed this test is about.
    monkeypatch.setattr(call_meter, "_dropped_at", call_meter.time.monotonic())
    assert call_meter.global_snapshot(series=False)["rate_capped"] is True
    call_meter.reset_all()
    assert call_meter.global_snapshot(series=False)["rate_capped"] is False


def test_every_failed_http_attempt_is_counted_at_the_wire(monkeypatch):
    """The wire path, end to end: three attempts made, three counted, three
    counted as failed, with the label the retry classifier used."""
    from aiforge_core.llm.client import _http

    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "3")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BASE_S", "0")
    monkeypatch.setattr(_http._rl, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(_http._rl, "acquire_global", lambda *a, **k: 0.0)
    monkeypatch.setattr(_http, "_preflight", lambda *_a: None)
    monkeypatch.setattr(_http, "_estimate_tokens", lambda *_a: 1)
    monkeypatch.setattr(_http.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ConnectionRefusedError("transient")))
    monkeypatch.setattr(_http.time, "sleep", lambda *_a: None)
    ep = _endpoint()
    with pytest.raises(ConnectionRefusedError):
        _http._post_with_retry(ep, b"{}", 1, role="doer",
                               source="test")
    g = call_meter.global_snapshot(series=False)
    assert g["total"] == 3
    assert g["failed"] == 3
    assert g["by_fail_reason"] == {"os_error": 3}


def test_a_successful_wire_call_is_not_counted_as_failed(monkeypatch):
    from aiforge_core.llm.client import _http

    class _Resp:
        def read(self):
            return b'{"choices": [{"message": {"content": "hi"}}]}'
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(_http._rl, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(_http._rl, "acquire_global", lambda *a, **k: 0.0)
    monkeypatch.setattr(_http, "_preflight", lambda *_a: None)
    monkeypatch.setattr(_http, "_estimate_tokens", lambda *_a: 1)
    monkeypatch.setattr(_http.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())
    _http._post(_endpoint(), b"{}", 1, role="doer")
    g = call_meter.global_snapshot(series=False)
    assert g["total"] == 1
    assert g["failed"] == 0


def test_a_token_that_says_no_session_is_not_second_guessed(monkeypatch):
    """A background fold's request is unattributed. By the time its failure
    surfaces the thread may have been rebound to a chat — reading the context
    THEN would bill a fold's timeout to whoever happens to be typing."""
    monkeypatch.delenv("AIFORGE_CURRENT_SESSION", raising=False)
    tok = call_meter.record("learner")          # no session bound
    token_ctx = request_context.set_session_id(77)
    try:
        call_meter.record_failure(tok, "timeout")
    finally:
        request_context.reset_session_id(token_ctx)
    assert call_meter.snapshot(77)["session_failed"] == 0
    assert call_meter.global_snapshot(series=False)["failed"] == 1


def test_an_empty_200_counts_as_a_failure_on_the_client_path(monkeypatch):
    """A 200-OK whose content is empty/think-only raises nothing, so the
    transport cannot see it — but it cost a generation and is about to be
    re-posted. Left uncounted, a wedged local model read "16 requests, 0
    failed" in chat while the pipeline meter, on the same box, read 75%
    failing."""
    from aiforge_core.llm import client as c
    import types as _types

    posts = {"n": 0}

    def _fake_post(ep, payload, timeout_s, *, role, source, meter=None):
        posts["n"] += 1
        if meter is not None:                      # count the send ourselves…
            meter[0] = call_meter.record(role, provider="x", model="m")
        # …then answer with nothing, twice, then for real.
        text = "" if posts["n"] < 3 else "the answer"
        return {"choices": [{"message": {"content": text}}]}

    monkeypatch.setattr(c, "_post_with_retry", _fake_post)
    monkeypatch.setattr(c, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(c.time, "sleep", lambda *_a: None)
    ep = _types.SimpleNamespace(model="m", provider="x", extras={},
                                base_url="http://x/v1")
    out = c._try_post(ep, [{"role": "user", "content": "q"}],
                      temperature=0.0, max_tokens=256, top_p=None, extras=None,
                      timeout_s=30, role="chat", source="primary")
    assert out is not None
    assert out[0] == "the answer"
    g = call_meter.global_snapshot(series=False)
    assert g["total"] == 3          # three generations paid for…
    assert g["failed"] == 2         # …two of which answered nothing
    assert g["by_fail_reason"] == {"empty": 2}


def test_a_call_stopped_before_it_is_sent_counts_nothing(monkeypatch):
    """Stop pressed while the request is still queued: nothing reaches the
    endpoint, so the meter must show neither a request nor a failure. Counting
    it would have the meter invent the traffic it exists to measure — and
    counting the `cancelled` exception without the send would put `failed`
    above `total`."""
    import threading
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.client._errors import _LLMCancelled

    monkeypatch.setattr(_http._rl, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(_http._rl, "acquire_global", lambda *a, **k: 0.0)
    monkeypatch.setattr(_http, "_preflight", lambda *_a: None)
    monkeypatch.setattr(_http, "_estimate_tokens", lambda *_a: 1)

    ev = threading.Event()
    ev.set()                                   # already stopped
    token = _http._CANCEL.set(ev)
    ep = _endpoint()
    try:
        with pytest.raises(_LLMCancelled):
            _http._post(ep, b"{}", 1, role="doer")
    finally:
        _http._CANCEL.reset(token)
    g = call_meter.global_snapshot(series=False)
    assert g["total"] == 0
    assert g["failed"] == 0


# ───────────────────────────── tokens ────────────────────────────────────
# Requests answer "how many calls did that cost". They cannot answer "how much
# did the model WRITE" — 40 one-line ReAct steps and one 6000-token essay are
# both "41 requests" — and that second question is the one a prompt asking for
# shorter answers is meant to move. Provider-reported, never estimated.


def test_tokens_are_counted_per_turn_chat_and_machine(monkeypatch):
    tok = call_meter.turn_reset(8)
    cv = call_meter.bind_turn(tok)
    t = call_meter.record("chat", session_id=8, now=1000.0)
    call_meter.record_tokens("chat", prompt_tokens=1200, completion_tokens=340,
                             token=t, now=1000.0)
    call_meter.reset_turn(cv)
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1001.0)

    snap = call_meter.snapshot(8)
    assert snap["turn_tokens_out"] == 340
    assert snap["turn_tokens_in"] == 1200
    assert snap["session_tokens_out"] == 340
    g = call_meter.global_snapshot(series=False)
    assert g["tokens_out"] == 340
    assert g["tokens_in"] == 1200
    assert g["tokens_out_60m"] == 340
    assert g["tokens_in_60m"] == 1200
    assert g["tokens_out_by_role"] == {"chat": 340}


def test_a_new_message_starts_its_token_count_from_zero():
    call_meter.turn_reset(9)
    t = call_meter.record("chat", session_id=9)
    call_meter.record_tokens("chat", completion_tokens=500, token=t)
    assert call_meter.snapshot(9)["turn_tokens_out"] == 500
    call_meter.turn_reset(9)
    snap = call_meter.snapshot(9)
    assert snap["turn_tokens_out"] == 0        # this message has written none…
    assert snap["session_tokens_out"] == 500   # …the chat still paid for it


def test_tokens_are_billed_to_the_minute_of_the_send(monkeypatch):
    """A long generation reports its tokens when it finishes; they belong to
    the minute whose traffic it was, exactly as a failure does."""
    t = call_meter.record("chat", now=1000.0)
    call_meter.record_tokens("chat", completion_tokens=900, token=t,
                             now=1000.0 + 400)
    monkeypatch.setattr(call_meter.time, "monotonic", lambda: 1000.0 + 401)
    g = call_meter.global_snapshot()
    assert g["tokens_out_60m"] == 900
    # Assert WHICH minute, not merely "some minute": settling 400s later is
    # still inside the same hour, so a total alone passes whether the tokens
    # land on the send's minute or the settle's.
    sent_at = [n for n, v in enumerate(g["series_60m"]) if v]
    tokens_at = [n for n, v in enumerate(g["series_token_out_60m"]) if v]
    assert sent_at
    assert tokens_at == sent_at


def test_a_response_with_no_usage_block_records_nothing():
    """Not every server reports usage. A zero must not be counted as a fact —
    "0 tokens written" is a claim, and a wrong one."""
    from aiforge_core.llm.client._helpers import _record_usage
    _record_usage("chat", {"choices": []})
    _record_usage("chat", {"usage": {}})
    _record_usage("chat", "not a dict")
    assert call_meter.global_snapshot(series=False)["tokens_out"] == 0


def test_usage_is_read_off_the_response_body():
    from aiforge_core.llm.client._helpers import _record_usage
    _record_usage("learner", {"usage": {"prompt_tokens": 90,
                                        "completion_tokens": 12}})
    g = call_meter.global_snapshot(series=False)
    assert g["tokens_in"] == 90
    assert g["tokens_out"] == 12


def test_record_tokens_never_raises_on_junk():
    call_meter.record_tokens("chat", prompt_tokens="x", completion_tokens=None)
    call_meter.record_tokens(None, prompt_tokens=-5, completion_tokens=-5)
    call_meter.record_tokens("chat", completion_tokens=7, token="junk")
    g = call_meter.global_snapshot(series=False)
    assert g["tokens_out"] == 7      # only the real one, and it still landed


def test_the_native_chat_path_bills_tokens_to_the_turn(monkeypatch):
    """`complete_raw` is the DEFAULT chat path (AIFORGE_CHAT_TOOL_PROTOCOL=
    native). It recorded usage without the meter token, so the tokens landed
    machine-wide and on NO turn: the chat badge and the persisted "⚡ N
    requests" line both read zero tokens for almost every real message while
    the session total climbed."""
    from aiforge_core.llm import client as c
    from aiforge_core.llm.types import Endpoint

    sid = 55
    tok = call_meter.turn_reset(sid)
    cv = call_meter.bind_turn(tok)
    monkeypatch.setattr(c, "resolve", lambda role: Endpoint(
        base_url="http://x/v1", api_key="", model="m", provider="p",
        role=role, extras={}))

    def _fake(ep, payload, timeout_s, *, role, source, meter=None):
        if meter is not None:
            meter[0] = call_meter.record(role, session_id=sid)
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 800, "completion_tokens": 250}}

    monkeypatch.setattr(c, "_post_with_retry", _fake)
    try:
        c.complete_raw("chat", [{"role": "user", "content": "q"}])
    finally:
        call_meter.reset_turn(cv)
    snap = call_meter.snapshot(sid)
    assert snap["turn_tokens_out"] == 250
    assert snap["turn_tokens_in"] == 800
