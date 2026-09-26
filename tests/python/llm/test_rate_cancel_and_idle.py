"""Stop ends a parked send without spending the window; the category cap is
read at the moment of the send; nothing throttles chat by default."""
import asyncio
import threading
import time

import pytest

from aiforge_core.llm import interactive_gate as gate
from aiforge_core.llm import rate_limiter as rl


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    # A server with spare slots: the idle lift below only happens there
    # (one slot keeps compaction capped; see test_llm_slots_compaction.py).
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "2")
    monkeypatch.setenv("AIFORGE_LLM_SHARED_WINDOW", "0")
    for var in ("AIFORGE_LLM_MAX_RPM", "AIFORGE_CHAT_RPM",
                "AIFORGE_COMPACTION_RPM"):
        monkeypatch.delenv(var, raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    rl.reset_global()
    yield
    rl.reset_global()


def _fake_clock(monkeypatch, on_sleep=None):
    clock = {"t": 10_000.0, "sleeps": 0}
    monkeypatch.setattr(rl.time, "monotonic", lambda: clock["t"])

    def _sleep(s):
        clock["t"] += s
        clock["sleeps"] += 1
        if on_sleep is not None:
            on_sleep(clock)
    monkeypatch.setattr(rl.time, "sleep", _sleep)
    return clock


def test_chat_is_never_parked_by_default():
    """A local model has no per-minute limit: 200 chat sends in a row go
    straight through with the default settings."""
    from aiforge_core.config import runtime_settings as rs
    rs.unset(["llm_max_rpm", "chat_rpm", "compaction_rpm"])
    t0 = time.monotonic()
    for _ in range(200):
        assert rl.acquire_global(role="doer", max_wait_s=120) == 0.0
    assert time.monotonic() - t0 < 2.0


def test_a_server_hold_still_applies_with_the_ceiling_off(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    rs.unset(["llm_max_rpm", "chat_rpm"])
    clock = _fake_clock(monkeypatch)
    rl.note_rate_limited(3.0, provider="p")
    t0 = clock["t"]
    rl.acquire_global(role="doer", provider="p", max_wait_s=30)
    assert clock["t"] - t0 >= 3.0


def test_a_cancelled_wait_returns_without_taking_a_slot(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 1, "chat_rpm": 0})
    stop = threading.Event()
    clock = _fake_clock(monkeypatch, on_sleep=lambda c: stop.set())
    assert rl.acquire_global(role="doer", max_wait_s=600) == 0.0
    assert rl.global_used() == 1
    t0 = clock["t"]
    rl.acquire_global(role="doer", max_wait_s=600, cancel=stop)
    assert clock["t"] - t0 <= 0.25 + 1e-9        # woke at the first check
    assert rl.global_used() == 1                  # no slot claimed
    assert rl.waiting() == 0


def test_an_already_cancelled_send_is_neither_throttled_nor_metered(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 1})
    stop = threading.Event()
    stop.set()
    recorded = []
    from aiforge_core.llm import call_meter
    monkeypatch.setattr(call_meter, "record", lambda **k: recorded.append(k))
    waited, tok = rl.govern_send(role="doer", cancel=stop)
    assert waited == 0.0 and tok is None and not recorded
    assert rl.global_used() == 0


def test_the_category_cap_is_read_again_during_a_wait(monkeypatch):
    """A compaction send parked at the busy-box cap is released as soon as
    the box goes idle, not after the whole 900s it was sized for."""
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 40, "compaction_rpm": 2, "chat_rpm": 0})
    gate.note_interactive()                       # a person is being served
    assert rl._category_limit("compaction") == 2

    def _idle(clock):
        gate.reset()                              # ... and then is not
    clock = _fake_clock(monkeypatch, on_sleep=_idle)
    for _ in range(2):
        assert rl.acquire_global(role="learner", max_wait_s=900) == 0.0
    t0 = clock["t"]
    rl.acquire_global(role="learner", max_wait_s=900)
    assert clock["t"] - t0 <= 5.0                 # one step, not ~60s
    assert rl._category_limit("compaction") == 40 - 10


def test_idle_compaction_leaves_chat_room_under_a_set_ceiling():
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 8, "compaction_rpm": 2, "chat_rpm": 0})
    lim = rl._category_limit("compaction")
    assert lim == 6                               # 8 - ceil(8 * 0.25)
    for _ in range(int(lim)):
        assert rl._take(8, "compaction", lim, "p")[0] is True
    assert rl._take(8, "compaction", lim, "p")[0] is False
    assert rl._take(8, "chat", 0, "p")[0] is True


def test_the_pipeline_throttle_stops_when_its_task_is_cancelled(monkeypatch):
    """ADK sends wait in a worker thread. Cancelling the task (Stop) must
    free that thread too, not leave it parked for the whole budget."""
    from aiforge_core.config import runtime_settings as rs
    from aiforge_core.runtime.escalating_llm import _wrapper
    rs.set_many({"llm_max_rpm": 1, "chat_rpm": 0})
    assert rl.acquire_global(role="doer", max_wait_s=1) == 0.0

    async def _run():
        task = asyncio.ensure_future(_wrapper._throttle_global("doer"))
        await asyncio.sleep(0.3)
        assert rl.waiting() == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    deadline = time.monotonic() + 3
    while rl.waiting() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert rl.waiting() == 0
    assert rl.global_used() == 1
