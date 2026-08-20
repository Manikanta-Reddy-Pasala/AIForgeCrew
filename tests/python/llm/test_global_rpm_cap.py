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
    from aiforge_core.config import _filecache
    _filecache.clear()
    rl._RPM_BUCKETS.clear()
    rl._TPM_BUCKETS.clear()
    yield
    rl._RPM_BUCKETS.clear()


def test_zero_means_no_ceiling(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_MAX_RPM", "0")
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 0})
    assert rl.global_rpm() == 0
    for _ in range(50):
        assert rl.acquire_global() == 0.0        # never blocks


def _fake_clock(monkeypatch):
    """A clock the test drives. The bucket refills with real seconds, so a
    no-op sleep would spin forever — advance the clock BY the sleep instead."""
    clock = {"t": 10_000.0}
    monkeypatch.setattr(rl.time, "time", lambda: clock["t"])
    monkeypatch.setattr(rl.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + s))
    return clock


def test_the_ceiling_queues_rather_than_fails(monkeypatch):
    """The bucket starts FULL, so a burst up to the ceiling goes straight
    through; the next call WAITS rather than erroring — a throttle, not a
    guard."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 3})
    clock = _fake_clock(monkeypatch)
    for _ in range(3):
        assert rl.acquire_global(max_wait_s=600) == 0.0
    t0 = clock["t"]
    waited = rl.acquire_global(max_wait_s=600)
    assert waited > 0                      # it queued…
    assert clock["t"] - t0 == waited       # …and the wait was real time
    assert waited <= 20.1                  # one token at 3/min


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
    assert warned and "rate_ceiling_overrun" in warned[0]


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
    rs.set_many({"llm_max_rpm": 240})          # a token every 0.25s
    for _ in range(240):
        rl.acquire_global(max_wait_s=5)        # drain: the bucket starts full
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
    assert rl._RPM_BUCKETS[rl._GLOBAL].capacity == 600


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
