"""The operator's calls-per-minute ceiling.

Not a guard — a THROTTLE. One agent turn is routinely 10-40 model calls, so a
low ceiling queues ordinary work rather than preventing anything; these pin
that it queues rather than fails, that it covers every caller, and that a
give-up stays retryable.
"""
import pytest

from aiforge_core.llm import rate_limiter as rl


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    # THE IN-PROCESS WINDOW, deliberately. It is the real fallback whenever the
    # shared store is unavailable (locked, read-only config dir, disabled), so
    # its math still has to hold — and it is the only half a driven monotonic
    # clock can exercise, since the cross-process window must use wall time.
    # The shared window has its own suite: test_shared_rate_window.py.
    monkeypatch.setenv("AIFORGE_LLM_SHARED_WINDOW", "0")
    # This file tests the GLOBAL ceiling in isolation. Neutralise the per-
    # category sub-ceilings (default chat=15/compaction=5) so they never
    # confound a pure-global assertion; the tests that exercise categories set
    # them explicitly.
    monkeypatch.setenv("AIFORGE_CHAT_RPM", "0")
    monkeypatch.setenv("AIFORGE_COMPACTION_RPM", "0")
    from aiforge_core.config import _filecache
    _filecache.clear()
    rl._RPM_BUCKETS.clear()
    rl._TPM_BUCKETS.clear()
    rl.reset_global()
    yield
    rl._RPM_BUCKETS.clear()
    rl.reset_global()


def test_zero_means_no_ceiling(monkeypatch):
    # "No ceiling at all" now means zeroing the global AND both category
    # sub-ceilings — they are independent knobs, and the default chat_rpm=15
    # would otherwise still throttle the un-roled calls below.
    monkeypatch.setenv("AIFORGE_LLM_MAX_RPM", "0")
    monkeypatch.setenv("AIFORGE_CHAT_RPM", "0")
    monkeypatch.setenv("AIFORGE_COMPACTION_RPM", "0")
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 0, "chat_rpm": 0, "compaction_rpm": 0})
    assert rl.global_rpm() == 0
    for _ in range(50):
        assert rl.acquire_global() == 0.0        # never blocks


def test_compaction_subceiling_independent_of_global(monkeypatch):
    # A compaction (learner) call is capped by compaction_rpm even when the
    # global ceiling has plenty of room — background folding must never crowd
    # out chat. With shared window off we exercise the in-process gate.
    monkeypatch.setenv("AIFORGE_LLM_SHARED_WINDOW", "0")
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 100, "compaction_rpm": 3, "chat_rpm": 100})
    rl.reset_global()
    for _ in range(3):
        assert rl._take(100, "compaction", 3, "p")[0] is True
    blocked, wait = rl._take(100, "compaction", 3, "p")
    assert blocked is False  # compaction bucket full
    assert wait > 0
    # chat is unaffected — its own bucket still has room under the same global
    assert rl._take(100, "chat", 100, "p")[0] is True


def _fake_clock(monkeypatch):
    """A clock the test drives. The window ages out in real seconds, so a
    no-op sleep would spin forever — advance the clock BY the sleep instead."""
    clock = {"t": 10_000.0}
    # MONOTONIC, because that is the clock the window runs on — wall time would
    # let one NTP step disable the ceiling (see rate_limiter._now).
    monkeypatch.setattr(rl.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(rl.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + s))
    return clock


def test_the_ceiling_queues_rather_than_fails(monkeypatch):
    """A burst up to the ceiling goes straight through; the next call WAITS
    rather than erroring — a throttle, not a guard."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 3})
    clock = _fake_clock(monkeypatch)
    for _ in range(3):
        assert rl.acquire_global(max_wait_s=600) == 0.0
    t0 = clock["t"]
    waited = rl.acquire_global(max_wait_s=600)
    assert waited > 0                      # it queued…
    assert clock["t"] - t0 == waited       # …and the wait was real time
    # …until the OLDEST of the three ages out of the 60s window. Not 20s: a
    # token bucket would drip one every 20s at 3/min and thereby allow 6 in
    # the first minute, which is the overrun this window exists to stop.
    assert abs(waited - 60.0) < 0.01


def test_never_more_than_the_ceiling_in_any_sixty_seconds(monkeypatch):
    """THE REGRESSION. The old token bucket started full and refilled while it
    was being drained, so a ceiling of N allowed N immediately plus N more
    during the same minute. Set to 17 against a provider counting 20/min, that
    sent ~34 and collected exactly the rejections the ceiling was set to
    prevent."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 17})
    clock = _fake_clock(monkeypatch)
    start = clock["t"]
    sends: list = []
    # Ask for 40 calls, allowing each to queue; count what leaves inside the
    # first 60 seconds of wall clock.
    for _ in range(40):
        rl.acquire_global(max_wait_s=600)
        sends.append(clock["t"])
    in_first_minute = [t for t in sends if t < start + 60.0]
    assert len(in_first_minute) == 17, in_first_minute
    # And the invariant holds for EVERY window, not just the first.
    for i, t in enumerate(sends):
        window = [u for u in sends if t - 60.0 < u <= t]
        assert len(window) <= 17, (i, len(window))


def test_a_server_rejection_makes_every_caller_hold(monkeypatch):
    """One rejection is the only ground truth we get about what the server is
    actually counting — our ceiling is per-process and cannot see the memory
    daemon. So a rejection holds the whole process instead of teaching
    nobody."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 10})
    _fake_clock(monkeypatch)
    assert rl.acquire_global(max_wait_s=600) == 0.0     # room to spare
    rl.note_rate_limited()
    assert abs(rl.held_for() - 60.0) < 0.01
    waited = rl.acquire_global(max_wait_s=600)
    assert abs(waited - 60.0) < 0.01                    # everyone holds


def test_a_rejection_with_retry_after_holds_only_that_long(monkeypatch):
    """Retry-After is the server telling us when its window clears. Charging a
    full minute anyway would idle the box for no reason."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 10})
    _fake_clock(monkeypatch)
    rl.note_rate_limited(5.0)
    waited = rl.acquire_global(max_wait_s=600)
    assert abs(waited - 5.0) < 0.01


def test_reset_global_clears_the_parked_counter(monkeypatch):
    rl._waiting = 3
    rl.reset_global()
    assert rl.waiting() == 0


def test_a_rejection_is_obeyed_even_with_no_ceiling_set(monkeypatch):
    """`llm_max_rpm=0` says "I have not asked you to throttle me" — a statement
    about OUR preference. It is not permission to ignore a provider that has
    just refused us, and 0 is the setting most operators run, so exempting it
    would leave the population that actually collects 400s unprotected."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 0})
    _fake_clock(monkeypatch)
    assert rl.acquire_global() == 0.0          # nothing counted, nothing held
    rl.note_rate_limited(30.0)
    waited = rl.acquire_global(max_wait_s=600)
    assert abs(waited - 30.0) < 0.01
    # …and no window is kept for an operator who asked for no ceiling.
    assert rl.global_used() == 0


def test_a_hostile_retry_after_cannot_park_the_whole_process(monkeypatch):
    """`Retry-After` is a number a REMOTE server chose. Unbounded, one header
    from a misconfigured or shared-tenant gateway parked every caller in the
    process for its full wait budget, once per call, for an hour."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 10})
    _fake_clock(monkeypatch)
    rl.note_rate_limited(3600.0)
    assert abs(rl.held_for() - 60.0) < 0.01          # AIFORGE_LLM_RATE_LIMIT_CAP_S


def test_the_hold_cap_is_operator_tunable(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_RATE_LIMIT_CAP_S", "300")
    _fake_clock(monkeypatch)
    rl.note_rate_limited(3600.0)
    assert abs(rl.held_for() - 300.0) < 0.01


def test_a_retry_after_under_the_cap_is_honoured_exactly(monkeypatch):
    _fake_clock(monkeypatch)
    rl.note_rate_limited(45.0)
    assert abs(rl.held_for() - 45.0) < 0.01


def test_a_hold_is_scoped_to_the_provider_that_rejected_us(monkeypatch):
    """The common setup is a cloud gateway for the doer and a local mlx server
    for the learner. A cloud 429 must not stall 60s of memory work against a
    server that declares no rate limit at all."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 0})
    _fake_clock(monkeypatch)
    rl.note_rate_limited(30.0, provider="openai")
    assert abs(rl.held_for("openai") - 30.0) < 0.01
    assert rl.held_for("mlx_lm") == 0.0
    assert rl.acquire_global(provider="mlx_lm") == 0.0
    assert abs(rl.acquire_global(provider="openai", max_wait_s=600) - 30.0) < 0.01


def test_a_hold_with_no_provider_applies_to_everyone(monkeypatch):
    """A caller that does not know its provider must not be exempt — the
    catch-all key is checked by every lookup."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 0})
    _fake_clock(monkeypatch)
    rl.note_rate_limited(20.0)
    assert abs(rl.held_for("anything") - 20.0) < 0.01


def test_a_rejection_during_an_overrun_burst_still_holds(monkeypatch):
    """THE REASON THE HOLD IS SEPARATE STATE. Waiters past max_wait_s each let
    themselves through, so the window sits AT OR ABOVE capacity — and "top the
    window up to capacity" is then range(0), a no-op at exactly the moment a
    rejection is most likely to arrive."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 3})
    _fake_clock(monkeypatch)
    for _ in range(3):
        rl.acquire_global(max_wait_s=600)
    for _ in range(4):                      # overrun: no budget to wait
        rl.acquire_global(max_wait_s=0.001)
    assert rl.global_used() >= 3            # window at/above capacity…
    rl.note_rate_limited(45.0)
    assert abs(rl.held_for() - 45.0) < 0.01  # …and the hold still bites
    assert rl.acquire_global(max_wait_s=600) >= 45.0


def test_an_overrun_call_is_counted_but_cannot_inflate_the_window(monkeypatch):
    """It left the box, so it is counted. But letting the window grow PAST
    capacity blocks the next well-behaved caller for a full 60s instead of
    60/rpm, and ships a `limit_used` above `limit_rpm` for the UI to render as
    nonsense."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 2})
    _fake_clock(monkeypatch)
    rl.acquire_global(max_wait_s=600)
    rl.acquire_global(max_wait_s=600)
    for _ in range(6):
        rl.acquire_global(max_wait_s=0.001)     # all overrun
    assert rl.global_used() == 2                # counted, never inflated


def test_a_fractional_ceiling_does_not_raise(monkeypatch):
    """int(0.9) is 0, which made `len(_sends) < 0` unreachable and dropped the
    function onto _sends[0] of an empty list. _http calls this BARE, so the
    IndexError hard-failed every LLM call on the box."""
    monkeypatch.setattr(rl, "global_rpm", lambda: 0.9)
    _fake_clock(monkeypatch)
    assert rl.acquire_global(max_wait_s=600) == 0.0     # the one send allowed
    assert abs(rl.acquire_global(max_wait_s=600) - 60.0) < 0.01


def test_a_backwards_clock_step_cannot_disable_the_ceiling(monkeypatch):
    """The window is on the MONOTONIC clock. On wall time, one NTP correction
    or a laptop resume left stamps that never aged out of `now - 60`: every
    caller took the overrun path and the ceiling was off for the length of the
    step, while logging that it was working."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 3})
    _fake_clock(monkeypatch)
    for _ in range(3):
        rl.acquire_global(max_wait_s=600)
    # Wall time lurches an hour backwards. Monotonic does not move.
    monkeypatch.setattr(rl.time, "time", lambda: 1.0)
    warned: list = []
    monkeypatch.setattr(rl.log, "warning",
                        lambda *a, **k: warned.append(a[0] if a else ""))
    waited = rl.acquire_global(max_wait_s=600)
    assert abs(waited - 60.0) < 0.01        # still throttled, exactly
    assert not warned                       # and no phantom overrun


def test_the_ceiling_delays_but_never_fails(monkeypatch):
    """The contract that separates this from the per-provider limiter.

    A provider limit is the provider's rule and exceeding it earns a 429, so
    that one raises. This ceiling is the operator's own preference — and
    raising on it made every short-deadline caller fail deterministically:
    the routers and classifiers run on 15-30s budgets, so at a low ceiling
    they would fail 100% of the time after the first minute, and one throttled
    call could kill a whole pipeline run. Past the wait budget it warns and
    lets the call through."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 1})
    rl.acquire_global(max_wait_s=600)
    warned: list = []
    monkeypatch.setattr(rl.log, "warning",
                        lambda *a, **k: warned.append(a[0] if a else ""))
    waited = rl.acquire_global(max_wait_s=0.001)   # no room at all
    assert waited >= 0.0                           # returned, did not raise
    assert warned
    assert "rate_ceiling_overrun" in warned[0]


def test_a_short_deadline_caller_is_never_failed_by_the_ceiling(monkeypatch):
    """The concrete regression: turn_router asks for 20s, the ceiling makes it
    wait, and that wait used to be charged against the caller's retry budget —
    so the classifier ERRORED instead of queueing. A real (short) wait here,
    driven by a real clock: the bucket refills in wall time and this test must
    not patch the shared time module out from under the transport.
    """
    from aiforge_core.config import runtime_settings as rs
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint
    rs.set_many({"llm_max_rpm": 240})          # 240 in any 60s
    seen: list = []
    monkeypatch.setattr(_http, "_preflight", lambda *_a: None)
    monkeypatch.setattr(_http, "_record_request", lambda *a, **k: None)
    monkeypatch.setattr(_http._rl, "acquire", lambda *a, **k: 0.0)

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"choices":[{"message":{"content":"ok"}}]}'
    monkeypatch.setattr(_http.urllib.request, "urlopen",
                        lambda *a, **k: (seen.append(1), _Resp())[1])
    ep = Endpoint(base_url="http://x/v1", api_key="", model="m",
                  provider="openai_compatible", role="triage", extras={})
    _http._post_with_retry(ep, b"{}", 20, role="triage", source="test")
    assert seen, "the call was failed by the ceiling instead of queued"


def test_raising_the_ceiling_takes_effect_without_a_restart(monkeypatch):
    """An operator who unblocks themselves in Settings should not have to wait
    out a bucket sized by the old number."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 1})
    _fake_clock(monkeypatch)
    rl.acquire_global(max_wait_s=600)
    assert abs(rl.acquire_global(max_wait_s=600) - 60.0) < 0.01   # 1/min
    rs.set_many({"llm_max_rpm": 600})
    assert rl.acquire_global(max_wait_s=600) <= 0.2      # 600/min: immediately
    # 3 sends happened, but the first aged out of the window during the 60s
    # wait — which is the window doing its job.
    assert rl.global_used() == 2


def test_the_meter_reports_the_ceiling_and_the_queue(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    from aiforge_core.llm import call_meter
    rs.set_many({"llm_max_rpm": 7})
    call_meter.reset_all()
    snap = call_meter.global_snapshot(series=False)
    assert snap["limit_rpm"] == 7
    assert snap["queued"] == 0
    call_meter.reset_all()


def test_the_wire_asks_the_global_limiter_first(monkeypatch):
    """A ceiling enforced per-provider only would miss the local provider,
    which declares no limits at all."""
    from aiforge_core.llm.client import _http
    from aiforge_core.llm.types import Endpoint
    seen: list = []
    monkeypatch.setattr(_http._rl, "acquire_global",
                        lambda **kw: seen.append(kw) or 0.0)
    monkeypatch.setattr(_http._rl, "acquire", lambda *a, **k: 0.0)
    monkeypatch.setattr(_http, "_preflight", lambda *_a: None)
    monkeypatch.setattr(_http, "_record_request", lambda *a, **k: None)

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"choices":[{"message":{"content":"hi"}}]}'
    monkeypatch.setattr(_http.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())
    ep = Endpoint(base_url="http://x/v1", api_key="", model="m",
                  provider="openai_compatible", role="doer", extras={})
    _http._post(ep, b"{}", 5, role="doer")
    assert seen, "the global ceiling was not consulted"


def test_the_window_holds_under_real_threads(monkeypatch):
    """`_sends` and `_holds` are shared mutable state and the release at the
    window edge is a thundering herd — every waiter wakes on the same instant
    and races the lock. The single-threaded tests above cannot see a check/append
    race, and the overrun paragraph in the docstring is *about* concurrency."""
    import threading
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 6})
    stamps: list = []
    lock = threading.Lock()

    def _go():
        # A budget far under the 60s the 7th..12th caller would need, so the
        # losers overrun rather than making the test take a minute.
        rl.acquire_global(max_wait_s=0.05)
        with lock:
            stamps.append(1)

    threads = [threading.Thread(target=_go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "a caller never returned"
    assert len(stamps) == 12                  # nobody was failed…
    assert rl.global_used() == 6              # …and the window is never inflated
    assert rl.waiting() == 0                  # …and the parked counter unwound


def test_a_hold_is_seen_by_threads_that_did_not_earn_it(monkeypatch):
    """The whole point of the hold: the caller that got rejected is not the one
    that needs to learn from it."""
    import threading
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 0})
    rl.note_rate_limited(30.0, provider="p")
    waited: list = []

    def _go():
        waited.append(rl.acquire_global(max_wait_s=0.05, provider="p"))

    ts = [threading.Thread(target=_go) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)
    assert len(waited) == 4
    assert rl.waiting() == 0


def test_govern_send_is_the_one_gateway_and_categorises_by_role(monkeypatch):
    """govern_send throttles under the role's category ceiling AND counts the
    send — the single place every model path routes through. A 'learner'
    (memory/compaction) send lands in the compaction bucket; a token comes back
    unless meter=False."""
    monkeypatch.setenv("AIFORGE_LLM_SHARED_WINDOW", "0")
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 100, "compaction_rpm": 2, "chat_rpm": 100})
    rl.reset_global()
    # learner → compaction bucket, capped at 2
    for _ in range(2):
        waited, tok = rl.govern_send(role="learner", provider="p", model="m")
        assert waited == 0.0
    assert sum(1 for _, c in rl._sends if c == "compaction") == 2
    blocked, _ = rl._take(100, "compaction", 2, "p")   # bucket now full
    assert blocked is False
    # a chat send is unaffected by the compaction cap
    waited, _ = rl.govern_send(role="doer", provider="p", model="m")
    assert waited == 0.0
    # meter=False → throttle only, no token
    _w, tok = rl.govern_send(role="doer", provider="p", meter=False)
    assert tok is None


def test_all_three_send_paths_route_through_govern_send():
    """Guard against a future path bypassing the ceiling+meter: the three model
    send paths must each call rate_limiter.govern_send."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3] / "aiforge_core"
    for rel in ("llm/client/_http.py",
                "integrations/instructor_adapter.py",
                "runtime/escalating_llm/_wrapper.py"):
        src = (root / rel).read_text()
        assert "govern_send(" in src, f"{rel} does not route through govern_send"
