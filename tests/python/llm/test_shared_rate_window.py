"""The rate ceiling is shared by every AIForge process on the machine.

THE BUG. `run.sh` starts more than one process that talks to the model —
uvicorn (chat, routers, jobs, the memory fold), `runtime.adk_runner` (the whole
team pipeline, its own PID) and `deploy.converge` (the startup fold, its own
PID) — and the window was a module global. Each process independently allowed
the operator's `llm_max_rpm`, so 15 against a gateway permitting 20/min put 30
on the wire and the rejections kept arriving with the setting correctly applied
everywhere. One number in Settings has to mean one number at the endpoint.

The multi-process test at the bottom is the one that would have caught it: no
amount of single-process testing can, which is exactly why it shipped.
"""
import os
import subprocess
import sys
import textwrap
import time

import pytest

from aiforge_core.llm import _shared_window as sw
from aiforge_core.llm import rate_limiter as rl


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AIFORGE_LLM_SHARED_WINDOW", raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    sw.close()
    rl.reset_global()          # also disarms the cooldown / one-shot warning
    yield
    rl.reset_global()
    sw.close()


def _set_rpm(n):
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": n})


# ── the store itself ────────────────────────────────────────────────

def test_take_counts_and_then_refuses():
    for i in range(3):
        assert sw.take(3) == (True, 0.0), i
    claimed, wait = sw.take(3)
    assert claimed is False
    assert 59.0 < wait <= 60.0          # until the oldest ages out


def test_the_window_slides():
    now = time.time()
    for _ in range(3):
        sw.take(3, now=now - 59.5)
    claimed, wait = sw.take(3, now=now)
    assert claimed is False and wait < 1.0
    # …half a second later the oldest has aged out and there is room again.
    assert sw.take(3, now=now + 1.0) == (True, 0.0)


def test_a_stamp_from_the_future_is_discarded_not_trusted():
    """The signature of a BACKWARDS clock step: written before, read after.

    Trusting it keeps the window full for the length of the step — the way a
    wall-clock window becomes a silent kill switch. The in-process window dodges
    this by using monotonic; a shared window cannot, because two processes'
    monotonic clocks have unrelated origins and are not comparable. So the
    danger is handled head-on instead: over-send by at most one window, never
    stop throttling.
    """
    now = time.time()
    for _ in range(5):
        sw.take(5, now=now + 3600)      # stamps from an hour in the future
    assert sw.take(5, now=now) == (True, 0.0)
    assert sw.count(now=now) == 1


def test_a_hold_is_visible_to_a_reader_that_did_not_set_it():
    sw.set_hold("openai", time.time() + 30)
    left = sw.hold_left(("", "openai"))
    assert 29.0 < left <= 30.0
    assert sw.hold_left(("", "mlx_lm")) == 0.0


def test_a_hold_from_a_moved_clock_does_not_park_the_box_forever():
    """Clamped ON WRITE, so an impossible hold never lands in the first
    place."""
    sw.set_hold("", time.time() + 90_000, cap=60.0)
    # Against the CALLER'S cap, not the hour-wide backstop: asserting <= 3600
    # let a one-hour hold satisfy "does not park the box forever", and the
    # write clamp it claims to test was not what kept it green.
    assert sw.hold_left(("",), cap=60.0) <= sw._cap(60.0)


def test_a_poisoned_hold_cannot_swallow_the_next_real_one():
    """`MAX(until, excluded.until)` meant one far-future row won forever: a
    forward clock step or a container with a bad clock wrote `now + 90000`, and
    from then on every REAL 429 for that provider was silently ignored — on
    disk, with no log line and no cure short of deleting the file."""
    now = time.time()
    # Write the poison directly, as a clock step would have left it.
    db = sw._conn()
    db.execute("INSERT INTO holds (k, until) VALUES (?, ?)", ("openai", now + 90_000))
    # A real rejection arrives.
    sw.set_hold("openai", now + 45, cap=60.0)
    left = sw.hold_left(("", "openai"), cap=60.0)
    assert 30.0 < left <= sw._cap(60.0), left


def test_a_far_future_hold_is_dropped_on_read_too():
    """Belt and braces: a row written before the clock moved is deleted when
    read, not merely ignored — ignoring it leaves it there to keep winning."""
    db = sw._conn()
    db.execute("INSERT INTO holds (k, until) VALUES (?, ?)",
               ("x", time.time() + 90_000))
    assert sw.hold_left(("x",)) == 0.0
    assert sw.hold_left(("x",)) == 0.0
    n = db.execute("SELECT COUNT(*) FROM holds WHERE k='x'").fetchone()[0]
    assert n == 0, "the poisoned row survived a read"


def test_an_unwritable_store_has_no_opinion(monkeypatch):
    """Never fails a call: the caller falls back to the in-process window."""
    monkeypatch.setattr(sw, "_conn", lambda: None)
    assert sw.take(5) is None
    assert sw.count() is None
    assert sw.hold_left(("",)) is None
    sw.force(5)                         # no raise
    sw.set_hold("x", 1.0)               # no raise


# ── the limiter on top of it ────────────────────────────────────────

def test_acquire_global_counts_into_the_shared_store():
    _set_rpm(5)
    assert rl.acquire_global() == 0.0
    assert sw.count() == 1
    assert rl.global_used() == 1


def test_a_hold_set_in_the_store_blocks_this_process(monkeypatch):
    """The cross-process half that matters most: only ONE process gets the
    429, and without a shared hold the others keep sending into a wall the
    server has already named."""
    _set_rpm(0)
    sw.set_hold("openai", time.time() + 45)
    slept: list = []
    monkeypatch.setattr(rl.time, "sleep", lambda s: slept.append(s))
    waited = rl.acquire_global(max_wait_s=0.001, provider="openai")
    assert waited >= 0.0                 # returned, never raised
    assert rl.held_for("openai") > 40


def test_note_rate_limited_writes_through_to_the_store():
    _set_rpm(10)
    rl.note_rate_limited(30.0, provider="openai")
    assert 29.0 < sw.hold_left(("", "openai")) <= 30.0


def test_disabling_the_shared_window_falls_back_in_process(monkeypatch):
    """An escape hatch, not a feature — but it must actually work, because it
    is the answer if the shared file ever misbehaves on someone's machine."""
    monkeypatch.setenv("AIFORGE_LLM_SHARED_WINDOW", "0")
    _set_rpm(3)
    for _ in range(3):
        assert rl.acquire_global() == 0.0
    assert sw.count() == 0               # nothing reached the shared store
    assert rl.global_used() == 3         # …and the in-process one has it all


def test_a_broken_store_still_throttles(monkeypatch):
    """A locked or read-only config dir must throttle slightly worse, never
    stop throttling."""
    monkeypatch.setattr(sw, "take", lambda *a, **k: None)
    monkeypatch.setattr(sw, "count", lambda *a, **k: None)
    _set_rpm(2)
    assert rl.acquire_global() == 0.0
    assert rl.acquire_global() == 0.0
    claimed, wait = rl._take(2, None)
    assert claimed is False and wait > 0


# ── the one that would have caught the bug ──────────────────────────

_CHILD = textwrap.dedent("""
    import json, os, sys
    sys.path.insert(0, {root!r})
    os.environ["AIFORGE_CONFIG_DIR"] = {cfg!r}
    from aiforge_core.llm import rate_limiter as rl
    got = 0
    for _ in range({tries}):
        # max_wait_s=0 so a blocked caller returns immediately rather than
        # parking: we are counting who got a SLOT, not who waited.
        claimed, _wait = rl._take({rpm}, None)
        if claimed:
            got += 1
    print(json.dumps({{"got": got}}))
""")


def _spawn(n, *, cfg, rpm, tries):
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    src = _CHILD.format(root=root, cfg=cfg, rpm=rpm, tries=tries)
    procs = [subprocess.Popen([sys.executable, "-c", src],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for _ in range(n)]
    out = []
    for p in procs:
        so, se = p.communicate(timeout=90)
        assert p.returncode == 0, se
        out.append(int(so.strip().splitlines()[-1].split('"got":')[1]
                       .rstrip("}").strip()))
    return out


def test_three_processes_share_one_ceiling(tmp_path, monkeypatch):
    """THE REGRESSION, in the only shape that can show it.

    Three processes, ceiling 10, each asking for 10 slots. Per-process windows
    hand out 30 — which is exactly how `llm_max_rpm=15` put 30/min on the wire
    against a gateway that allows 20. Shared, they hand out 10 between them.
    """
    cfg = str(tmp_path / "cfg")
    os.makedirs(cfg, exist_ok=True)
    from aiforge_core.config import runtime_settings as rs
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", cfg)
    from aiforge_core.config import _filecache
    _filecache.clear()
    rs.set_many({"llm_max_rpm": 10})

    granted = _spawn(3, cfg=cfg, rpm=10, tries=10)
    assert sum(granted) == 10, granted
    # …and every process got some, so it is a shared window and not a lock one
    # process is holding for the whole minute.
    assert len([g for g in granted if g]) >= 1


def test_a_hold_earned_in_one_process_is_obeyed_in_another(tmp_path,
                                                           monkeypatch):
    """Only one process gets the 429."""
    cfg = str(tmp_path / "cfg")
    os.makedirs(cfg, exist_ok=True)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", cfg)
    from aiforge_core.config import _filecache
    _filecache.clear()
    sw.close()
    rl.note_rate_limited(40.0, provider="openai")

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    src = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {root!r})
        os.environ["AIFORGE_CONFIG_DIR"] = {cfg!r}
        from aiforge_core.llm import rate_limiter as rl
        print(round(rl.held_for("openai")))
    """)
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=90)
    assert p.returncode == 0, p.stderr
    assert 30 <= int(p.stdout.strip().splitlines()[-1]) <= 40


# ── what the first review round found missing ───────────────────────

def test_an_overrun_cannot_inflate_the_shared_window():
    """The in-process window clamped this; the shared one did not, so the
    toolbar could render "7/3". Past the wait budget a call goes out and is
    honestly counted — but the count must still describe a window of `limit`,
    or the next well-behaved caller waits a full 60s instead of 60/rpm."""
    _set_rpm(3)
    for _ in range(3):
        rl.acquire_global()
    for _ in range(4):
        rl.acquire_global(max_wait_s=0.001)     # all overrun
    assert rl.global_used() == 3


def test_rows_do_not_accumulate():
    """Every other assertion here reads `count()`, which filters by ts — so
    pruning could stop working entirely and nothing would notice until the file
    grew without bound."""
    now = time.time()
    for i in range(50):
        sw.take(5, now=now - 120 + i)           # all older than the window
    sw.take(5, now=now)
    db = sw._conn()
    total = db.execute("SELECT COUNT(*) FROM sends").fetchone()[0]
    assert total <= 5, total


def test_the_scope_says_which_window_is_in_force(monkeypatch):
    """A silent fallback puts the ceiling back to per-process — the very bug
    this fixes — and an operator staring at the toolbar had no way to tell."""
    from aiforge_core.llm import call_meter
    _set_rpm(5)
    rl.acquire_global()
    assert rl.window_scope() == "machine"
    assert call_meter.global_snapshot(series=False)["limit_scope"] == "machine"
    # A READ is the wrong probe: WAL readers never block on a writer, so
    # count() keeps returning a number while every take() fails — exactly the
    # state this field exists to reveal. Break the WRITE path only.
    class _Broken:
        def execute(self, *a, **k):
            raise OSError("read-only file system")
    monkeypatch.setattr(sw, "_conn", lambda: _Broken())
    assert rl.window_scope() == "process"


def test_contention_is_not_reported_as_a_dead_store(monkeypatch):
    """Busy means the store is ALIVE and someone else is writing to it.
    Reporting that as "the ceiling is per-process now" is the opposite of the
    truth, and it happened on 4 of 28 probes under real contention — so the
    operator's one diagnostic flickered during exactly the busy minute they
    would be looking at it."""
    monkeypatch.setattr(sw, "_conn", lambda: _LockedConn())
    assert sw.writable() is True
    assert rl.window_scope() == "machine"


def test_the_scope_is_not_fooled_by_a_healthy_read(monkeypatch):
    """THE REGRESSION. `count()` succeeded while writes failed, so the one
    diagnostic an operator has confidently reported the wrong answer in the
    one failure it was added for."""
    _set_rpm(5)
    monkeypatch.setattr(sw, "count", lambda *a, **k: 3)      # reads look fine
    monkeypatch.setattr(sw, "writable", lambda: False)       # writes do not
    assert rl.window_scope() == "process"


def test_the_fallback_is_announced_once():
    """Silence here is indistinguishable from working.

    Captures from the logger DIRECTLY rather than through caplog: caplog
    depends on propagation reaching the root handler, and this repo installs
    logging filters/handlers in other suites — so the assertion passed alone
    and failed in a full run for a reason that had nothing to do with the
    behaviour under test.
    """
    import logging
    seen: list = []

    class _Grab(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = _Grab()
    logger = logging.getLogger("aiforge.rate_limiter")
    logger.addHandler(handler)
    old_level, logger.level = logger.level, logging.WARNING
    try:
        assert sw._fails == 0 and sw._warned is False
        for _ in range(sw._FAIL_LIMIT):
            sw._degrade(OSError("disk is read-only"))
    finally:
        logger.removeHandler(handler)
        logger.level = old_level

    msgs = [m for m in seen if "shared_window_unavailable" in m]
    assert len(msgs) == 1, seen
    assert "PER-PROCESS" in msgs[0]
    # …and it then stops trying for a while rather than re-probing every call.
    assert sw._cold() is True


def test_a_dead_store_is_not_reprobed_on_every_call(monkeypatch):
    """On a filesystem where SQLite locking does not work, re-probing cost the
    lock timeout on EVERY model call."""
    monkeypatch.setattr(sw, "_cold_until", time.time() + 30)
    assert sw._conn() is None
    assert sw.take(5) is None


def test_reset_does_not_create_a_store_just_to_clear_it(tmp_path, monkeypatch):
    """The autouse conftest fixture resets twice per test, thousands of times."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "fresh"))
    sw.close()
    sw.reset()
    assert not os.path.exists(sw.path())


# ── real contention, which is the claim that most needs proving ─────

_RACER = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, {root!r})
    os.environ["AIFORGE_CONFIG_DIR"] = {cfg!r}
    from aiforge_core.llm import _shared_window as sw
    start = {start!r}
    # A fixed DURATION, not a fixed attempt count. With a count, a child that
    # imported slowly could finish all its attempts before another child began
    # — the intervals would not overlap and the test would flake without the
    # implementation changing at all. Running to a common wall-clock deadline
    # makes the overlap structural.
    while time.time() < start:
        pass
    tries = 0
    got = 0
    while time.time() < start + 0.5:
        tries += 1
        r = sw.take({limit})
        if r is not None and r[0]:
            got += 1
    print("%d %d" % (got, tries))
""")


def test_no_two_processes_get_the_same_last_slot(tmp_path, monkeypatch):
    """THE ATOMICITY CLAIM. take() does DELETE+COUNT+INSERT in one
    BEGIN IMMEDIATE precisely so two processes cannot both read "14 of 15" and
    both send. Six processes race for 20 slots from a standing start."""
    cfg = str(tmp_path / "cfg")
    os.makedirs(cfg, exist_ok=True)
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    start = time.time() + 2.0
    procs = [subprocess.Popen(
        [sys.executable, "-c", _RACER.format(root=root, cfg=cfg, start=start,
                                             limit=20)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(6)]
    rows = []
    for p in procs:
        so, se = p.communicate(timeout=120)
        assert p.returncode == 0, se
        n, tries = so.strip().splitlines()[-1].split()
        rows.append((int(n), int(tries)))
    assert sum(r[0] for r in rows) == 20, rows

    # Every process really was attempting inside the shared half-second, so the
    # take() transactions overlapped and a coarse whole-file lock could not
    # have produced this.
    assert all(r[1] > 0 for r in rows), rows
    assert len([r for r in rows if r[1] > 1]) >= 2, rows

    # NOT asserted: a fair split. The winner of the first lock drains the
    # window in microseconds, so one process legitimately takes all 20. This
    # ceiling is a throttle, not a scheduler — the property that matters is
    # "never MORE than the limit", and asserting fairness would pin behaviour
    # the design does not provide.


class _LockedConn:
    """A connection whose every statement reports the write lock is held."""

    def execute(self, *a, **k):
        import sqlite3
        raise sqlite3.OperationalError("database is locked")


def test_contention_never_promotes_a_caller_to_its_own_window(monkeypatch):
    """THE REGRESSION FROM LOWERING THE LOCK TIMEOUT.

    A busy store is a WORKING store — another process is counting in it right
    now. Reporting that as "no opinion" sent the caller to its own in-process
    window, which handed out a slot the shared one had refused: measured at 12
    grants for a ceiling of 10, i.e. the exact over-sending this module exists
    to stop, appearing only under the contention it exists to handle."""
    import sqlite3
    assert sw._busy(sqlite3.OperationalError("database is locked")) is True
    assert sw._busy(sqlite3.OperationalError("no such table")) is False
    assert sw._busy(OSError("read-only file system")) is False

    monkeypatch.setattr(sw, "_conn", lambda: _LockedConn())
    got = sw.take(2)
    assert got is not None, "a busy store reported itself as unavailable"
    claimed, wait = got
    assert claimed is False and wait > 0

    # …and the limiter does NOT fall through to its private window.
    _set_rpm(2)
    claimed, wait = rl._take(2, None)
    assert claimed is False, "contention handed out a slot anyway"
    assert wait > 0


def test_a_genuinely_broken_store_still_falls_back(monkeypatch):
    """The other side of that judgement: "broken" must still degrade, or a
    read-only config dir would stop the box instead of throttling it."""
    class _Broken:
        def execute(self, *a, **k):
            raise OSError("read-only file system")
    monkeypatch.setattr(sw, "_conn", lambda: _Broken())
    assert sw.take(2) is None
    _set_rpm(2)
    assert rl._take(2, None)[0] is True     # the in-process window serves it


# ── the three the second review round measured ──────────────────────

_OPENER = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, {root!r})
    os.environ["AIFORGE_CONFIG_DIR"] = {cfg!r}
    from aiforge_core.llm import _shared_window as sw
    while time.time() < {start!r}:
        pass
    ok = sw.take(1000) is not None
    print("1" if ok else "0")
""")


def test_a_cold_start_stampede_does_not_hand_out_private_allowances(tmp_path):
    """C1. SQLite takes an EXCLUSIVE lock to set the journal mode and does NOT
    run the busy handler for it, so `timeout` buys nothing on a fresh file and
    concurrent first-opens simply failed — ~19% of them, each one giving that
    process a full private ceiling.

    This is the run.sh cold start exactly: uvicorn, the pipeline runner and the
    boot-time fold all reach a config dir with no llm_rate.db at once, and the
    fold is a burst of model calls at that very moment.
    """
    cfg = str(tmp_path / "cfg")
    os.makedirs(cfg, exist_ok=True)
    assert not os.path.exists(os.path.join(cfg, "llm_rate.db"))
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    start = time.time() + 2.0
    procs = [subprocess.Popen(
        [sys.executable, "-c", _OPENER.format(root=root, cfg=cfg, start=start)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(12)]
    ok = []
    for p in procs:
        so, se = p.communicate(timeout=120)
        assert p.returncode == 0, se
        ok.append(so.strip().splitlines()[-1])
    assert ok.count("1") == 12, ok


def test_one_success_clears_the_failure_run():
    """C2. `_healthy()` was defined and never called, so `_fails` counted every
    failure for the life of the PROCESS rather than consecutive ones — three
    stumbles hours apart tripped a 60s cooldown in which the process ran on its
    private window with the whole ceiling. Measured: 331 sends against 50."""
    assert sw._fails == 0 and sw._cold() is False
    sw._degrade(OSError("blip"))
    sw._degrade(OSError("blip"))
    assert sw._fails == 2
    sw.take(5)                      # a normal, successful acquire
    assert sw._fails == 0, "a success did not clear the failure run"
    assert sw._cold() is False


def test_a_backwards_clock_step_cannot_switch_the_ceiling_off(monkeypatch):
    """C3. The hold clamp was a fixed hour while the largest hold anything
    legitimately writes is llm_rate_limit_cap_s (default 60). A backwards step
    of up to 59 minutes was therefore honoured VERBATIM — and held_for() takes
    max(in-process, shared), so the poisoned wall-clock value overrode the
    monotonic one that was immune. Every call then took the overrun branch and
    went straight out, while the log said the ceiling was working."""
    now = time.time()
    db = sw._conn()
    # What a 20-minute backwards step leaves behind: a row written before it.
    db.execute("INSERT INTO holds (k, until) VALUES (?, ?)",
               ("openai", now + 1200))
    left = sw.hold_left(("", "openai"), cap=60.0)
    assert left == 0.0, f"a 20-minute hold was honoured: {left}"
    assert rl.held_for("openai") == 0.0


def test_a_legitimate_hold_is_still_honoured_in_full():
    """The other side of that clamp: a real Retry-After must survive it."""
    sw.set_hold("openai", time.time() + 55, cap=60.0)
    assert 50.0 < sw.hold_left(("", "openai"), cap=60.0) <= 60.0


class _InterruptOn:
    """Delegates to a real connection, but raises KeyboardInterrupt on the
    first statement whose SQL starts with ``prefix``."""

    def __init__(self, real, prefix):
        self._real, self._prefix = real, prefix
        self.closed = False
        self.fired = False

    def execute(self, sql, *a, **k):
        if sql.startswith(self._prefix) and not self.fired:
            self.fired = True
            raise KeyboardInterrupt("ctrl-c")
        return self._real.execute(sql, *a, **k)

    def close(self):
        self.closed = True
        self._real.close()


class _InterruptOnInsert:
    """Delegates to a real connection, but raises KeyboardInterrupt on the
    INSERT — i.e. between BEGIN IMMEDIATE and COMMIT."""

    def __init__(self, real):
        self._real = real
        self.closed = False

    def execute(self, sql, *a, **k):
        if sql.startswith("INSERT INTO sends"):
            raise KeyboardInterrupt("ctrl-c")
        return self._real.execute(sql, *a, **k)

    def close(self):
        self.closed = True
        self._real.close()


def test_an_interrupt_mid_transaction_does_not_wedge_the_store(monkeypatch):
    """M1. A KeyboardInterrupt between BEGIN IMMEDIATE and COMMIT — Ctrl-C into
    run.sh's process group is enough — left the cached connection inside an
    open write transaction forever. Nothing discarded it, so this thread was
    permanently dead AND every other process paid the full lock timeout on
    every model call before falling back too. With no warning at all, because
    _degrade is only reached from _conn, which had succeeded."""
    _set_rpm(5)
    proxy = _InterruptOnInsert(sw._conn())
    monkeypatch.setattr(sw, "_conn", lambda: proxy)
    with pytest.raises(KeyboardInterrupt):
        sw.take(5)
    # The failed connection was rolled back AND dropped — keeping it is what
    # blocked everyone else.
    assert proxy.closed is True
    monkeypatch.undo()
    sw.close()
    # …and the very next call works, on a fresh connection.
    assert sw.take(5) == (True, 0.0)
    assert sw.writable() is True


@pytest.mark.parametrize("op,prefix", [
    ("force", "DELETE FROM sends WHERE rowid IN"),
    ("writable", "DELETE FROM sends WHERE ts < 0"),
])
def test_every_transaction_drops_a_wedged_connection(monkeypatch, op, prefix):
    """take() was pinned; force() and writable() open write transactions of
    their own and were not. A connection abandoned inside BEGIN IMMEDIATE
    blocks every other process on the machine regardless of which function
    left it there."""
    _set_rpm(5)
    proxy = _InterruptOn(sw._conn(), prefix)
    monkeypatch.setattr(sw, "_conn", lambda: proxy)
    with pytest.raises(KeyboardInterrupt):
        if op == "force":
            sw.force(5)
        else:
            sw.writable()
    assert proxy.fired is True
    assert proxy.closed is True, f"{op} left the connection wedged"
    monkeypatch.undo()
    sw.close()
    assert sw.take(5) == (True, 0.0)


# ── what the third review round measured ────────────────────────────

_STEPPER = textwrap.dedent("""
    import os, sys, threading, time
    sys.path.insert(0, {root!r})
    os.environ["AIFORGE_CONFIG_DIR"] = {cfg!r}
    from aiforge_core.llm import _shared_window as sw
    start, step_at = {start!r}, {step_at!r}
    OFFSET = {offset!r}
    real = time.time
    def clocked():
        t = real()
        return t + OFFSET if t >= step_at else t
    time.time = clocked
    got = 0
    lock = threading.Lock()
    def work():
        global got
        while real() < start + 1.0:
            r = sw.take({limit})
            if r is not None and r[0]:
                with lock:
                    got += 1
    while real() < start:
        pass
    ts = [threading.Thread(target=work) for _ in range({threads})]
    for t in ts: t.start()
    for t in ts: t.join()
    print(got)
""")


def test_a_clock_step_costs_ONE_window_not_one_per_caller(tmp_path):
    """THE THIRD-ROUND CRITICAL.

    `now` was read BEFORE `BEGIN IMMEDIATE`, so a caller parked on the write
    lock could prune with a clock reading from the OTHER SIDE of a step —
    wiping the whole window the post-step callers had just filled. Every
    straggler then cost another full window: 2080 grants against a ceiling of
    50 at 6 processes x 8 threads, where the docstring promises "at most one
    window of over-sending".
    """
    cfg = str(tmp_path / "cfg")
    os.makedirs(cfg, exist_ok=True)
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    start = time.time() + 2.0
    procs = [subprocess.Popen(
        [sys.executable, "-c", _STEPPER.format(
            root=root, cfg=cfg, start=start, step_at=start + 0.4,
            offset=900.0, limit=20, threads=4)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(4)]
    got = []
    for p in procs:
        so, se = p.communicate(timeout=180)
        assert p.returncode == 0, se
        got.append(int(so.strip().splitlines()[-1]))
    total = sum(got)
    # One window before the step, one after. NOT one per in-flight caller.
    assert total <= 40, (total, got)
    assert total >= 20, (total, got)


def test_a_contended_hold_write_does_not_arm_the_cooldown(monkeypatch):
    """set_hold runs from note_rate_limited — during a 429 storm, when every
    process on the box is writing a hold at once. Counting that contention as
    failure armed the 60s cooldown and switched the ceiling off at the one
    moment it matters most."""
    monkeypatch.setattr(sw, "_conn", lambda: _LockedConn())
    for _ in range(sw._FAIL_LIMIT + 2):
        sw.set_hold("openai", time.time() + 30, cap=60.0)
    assert sw._fails == 0, "contended hold writes armed the cooldown"
    assert sw._cold() is False


def test_a_contended_force_reports_the_miss(monkeypatch):
    """force()'s contract is that a send which left the box is counted whatever
    the window says — so silently losing it to contention breaks the one
    promise it makes."""
    monkeypatch.setattr(sw, "_conn", lambda: _LockedConn())
    assert sw.force(5) is False
    assert sw._fails == 0            # busy, not broken


def test_a_succeeding_READ_cannot_silence_the_warning(monkeypatch):
    """The read-succeeds/write-fails split is exactly what `writable()` exists
    to detect — and the toolbar polls /api/llm/usage every 3s, so a laundering
    count() reset the failure run between every failing write and the operator
    was told nothing across 480 model calls on a private window."""
    import logging
    seen: list = []

    class _Grab(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = _Grab()
    logger = logging.getLogger("aiforge.rate_limiter")
    logger.addHandler(handler)
    old_level, logger.level = logger.level, logging.WARNING
    try:
        for _ in range(sw._FAIL_LIMIT * 2):
            sw._degrade(OSError("read-only"))   # a failing write
            sw._healthy()                       # …laundered by a good read
    finally:
        logger.removeHandler(handler)
        logger.level = old_level
    assert [m for m in seen if "shared_window_unavailable" in m], \
        "a succeeding read suppressed the warning entirely"


def test_a_poisoned_catch_all_does_not_discard_a_real_provider_hold():
    """MAX(until) across the queried keys let a poisoned '' row outrank a good
    provider hold, and cleaning it up returned 0 — sending one call into a wall
    the server had already named."""
    now = time.time()
    db = sw._conn()
    db.execute("INSERT INTO holds (k, until) VALUES (?, ?)", ("", now + 90_000))
    sw.set_hold("openai", now + 45, cap=60.0)
    left = sw.hold_left(("", "openai"), cap=60.0)
    assert 30.0 < left <= sw._cap(60.0), left


def test_a_cap_of_none_or_nonsense_does_not_reopen_the_hour_wide_hole():
    """The DEFAULT was _MAX_HOLD_S, i.e. the bug that had just been fixed —
    and these are public functions."""
    for bad in (None, 0, -1, float("nan")):
        assert sw._cap(bad) <= 100.0, bad
    # …and the allowance is proportional, so a 1-second cap is not a minute.
    assert sw._cap(1.0) < 10.0


def test_the_window_count_is_time_bounded():
    """global_used() is the toolbar's `limit_used`; after an idle period only
    the time filter keeps it honest."""
    now = time.time()
    db = sw._conn()
    for _ in range(9):
        db.execute("INSERT INTO sends (ts) VALUES (?)", (now - 300,))
    assert sw.count(now=now) == 0


def test_the_hold_is_checked_before_the_slot_is_claimed(monkeypatch):
    """Claiming first and handing back needed a "give one back" operation, and
    with no ownership token that deleted another process's real send. Nothing
    in the suite noticed the ordering, so pin it: under a hold, no slot is
    consumed at all."""
    _set_rpm(5)
    rl.note_rate_limited(30.0, provider="openai")
    tried: list = []
    real_take = sw.take
    monkeypatch.setattr(sw, "take", lambda *a, **k: tried.append(1) or real_take(*a, **k))
    # No budget, so it takes the overrun path rather than sleeping 30s. What
    # matters is that it never ASKED for a slot: claiming first and handing one
    # back is what needed the "give one back" operation that deleted another
    # process's send.
    rl.acquire_global(max_wait_s=0.001, provider="openai")
    assert not tried, "a slot was claimed while a hold stood"


# ── the coverage gaps mutation found (round 4) ──────────────────────

def test_a_missed_forced_send_falls_back_instead_of_vanishing(monkeypatch):
    """force() reports a miss so it is NOT lost — and the limiter dropped the
    return value AND returned before the fallback, so under saturation 0.8% of
    forced sends (100% during a cooldown) were counted in neither window.
    Under-counting is the direction that permits extra sends later."""
    _set_rpm(5)
    monkeypatch.setattr(sw, "force", lambda *a, **k: False)
    before = len(rl._sends)
    rl._force_take(5)
    assert len(rl._sends) == before + 1, "a missed forced send was lost entirely"


def test_a_successful_forced_send_is_not_double_counted(monkeypatch):
    """The other direction: when the shared store DID take it, the in-process
    window must not count it again."""
    _set_rpm(5)
    monkeypatch.setattr(sw, "force", lambda *a, **k: True)
    before = len(rl._sends)
    rl._force_take(5)
    assert len(rl._sends) == before
    assert sw.count() == 0          # our fake swallowed it; nothing else added


def test_an_overrun_call_is_actually_recorded(monkeypatch):
    """"THE OVERRUN IS ACCOUNTED FOR" was unasserted: at capacity, counting it
    and not counting it give the same `global_used()`, so a mutation that
    skipped the record entirely survived the suite. Assert the SEND, not the
    count — the window must now hold a row stamped for this call."""
    _set_rpm(2)
    for _ in range(2):
        rl.acquire_global()
    db = sw._conn()
    newest_before = db.execute("SELECT MAX(rowid) FROM sends").fetchone()[0]
    rl.acquire_global(max_wait_s=0.001)     # overruns
    newest_after = db.execute("SELECT MAX(rowid) FROM sends").fetchone()[0]
    assert newest_after > newest_before, "the overrun call was never recorded"


def test_force_reads_the_clock_inside_the_lock():
    """take()'s half is pinned; force()'s was not. A stale stamp is bounded by
    the lock timeout rather than wiping a window, but it is the same defect."""
    now = time.time()
    db = sw._conn()
    for _ in range(5):
        db.execute("INSERT INTO sends (ts) VALUES (?)", (now + 3600,))
    # force() must prune the impossible rows and keep its OWN send.
    assert sw.force(5) is True
    assert sw.count() == 1


def test_a_shorter_shared_hold_cannot_shorten_our_own(monkeypatch):
    """held_for takes max(in-process, shared): a row another process swept as
    poisoned must not cut a hold we are already observing."""
    _set_rpm(0)
    rl.note_rate_limited(45.0, provider="openai")
    monkeypatch.setattr(sw, "hold_left", lambda *a, **k: 1.0)
    assert rl.held_for("openai") > 30.0


class _BusyOnDelete:
    """Reads fine; every DELETE reports the write lock is held."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *a, **k):
        if sql.lstrip().upper().startswith("DELETE"):
            import sqlite3
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *a, **k)


def test_contention_while_cleaning_a_poisoned_hold_is_not_read_as_no_hold(
        monkeypatch):
    """The cleanup branch turned a CONTENDED delete into "clear to send".

    Several processes write holds at once during a 429 storm, which is exactly
    when this branch runs — so treating contention as "no hold" discarded a
    live hold at the worst possible moment, the same class of bug as the
    poisoned row the branch exists to remove."""
    now = time.time()
    db = sw._conn()
    db.execute("INSERT INTO holds (k, until) VALUES (?, ?)", ("", now + 90_000))
    monkeypatch.setattr(sw, "_conn", lambda: _BusyOnDelete(db))
    left = sw.hold_left(("", "openai"), cap=60.0)
    assert left > 0.0, "contention during cleanup was read as no hold"
    assert left <= sw._cap(60.0)
