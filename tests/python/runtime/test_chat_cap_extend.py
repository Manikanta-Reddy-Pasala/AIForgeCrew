"""Runaway cap / turn deadline EXTENSIONS.

The step cap and the wall-clock deadline are runaway guards, not task budgets.
A turn that is still producing new work condenses its history and extends
itself (bounded by ``chat_cap_extensions``); a turn that is only spinning is
stopped exactly as before, and a caller-set ``max_steps`` is never extended.
"""
import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime.chat_agent._context import _limits

# Extensions are for INTERACTIVE turns, so every extension test needs a session.
_SID = 991_001


def _churner():
    """The RUNAWAY: novel tool args every step, but it reads nothing new and
    changes nothing. Distinct-action counting would call this "progress" — it
    is exactly what the deadline was written to stop."""
    calls = {"n": 0}

    def fn(role, messages, **kw):
        calls["n"] += 1
        return f'ACTION: run_command\nARGS_JSON: {{"command": "echo {calls["n"]}"}}'
    return fn, calls


def _reader(tmp_path, files=400):
    """REAL work: reads a file it has never read before on every step."""
    for i in range(files):
        (tmp_path / f"f{i}.txt").write_text(f"contents {i}")
    calls = {"n": 0}

    def fn(role, messages, **kw):
        calls["n"] += 1
        return ('ACTION: file_read\nARGS_JSON: '
                f'{{"path": "f{calls["n"]}.txt"}}')
    return fn, calls


def _msgs(evs, kind="message"):
    return " ".join(e.get("text", "") for e in evs if e["type"] == kind)


# ── step cap ────────────────────────────────────────────────────────────

def test_step_cap_extends_a_progressing_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "2")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "2")
    fn, calls = _reader(tmp_path)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "long job"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    # 2 steps + 2 extensions × 2 steps = 6 model calls, then the hard stop.
    assert calls["n"] == 6
    assert "extended the step budget" in _msgs(evs, "thought")
    assert "runaway safety cap" in _msgs(evs)
    assert evs[-1] == {"type": "done"}


def test_extensions_zero_is_the_old_hard_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "2")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "0")
    fn, calls = _reader(tmp_path)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "long job"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    assert calls["n"] == 2
    assert "extended the step budget" not in _msgs(evs, "thought")
    assert "runaway safety cap" in _msgs(evs)


def test_stop_banner_points_at_the_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "1")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "0")
    fn, _calls = _churner()
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "x"}], cwd=str(tmp_path), complete_fn=fn,
        session_id=_SID))
    text = _msgs(evs)
    assert text.startswith("(stopped:")          # quality_gate's incomplete mark
    assert "Settings" in text and "AIFORGE_CHAT_SAFETY_CAP" in text


def test_a_churning_agent_earns_nothing(tmp_path, monkeypatch):
    """Novel `run_command` args are not progress: the agent reads nothing new
    and edits nothing, so it is stopped at the cap however many extensions the
    operator allowed. This is the case the whole guard exists for."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "3")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "5")
    fn, calls = _churner()
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "spin"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    assert calls["n"] == 3                       # no extension at all
    assert "extended the step budget" not in _msgs(evs, "thought")
    assert "runaway safety cap" in _msgs(evs)


def test_re_reading_the_same_file_is_not_new_work(tmp_path, monkeypatch):
    """A turn burning steps on a read it ALREADY ran learns nothing new: the
    first read earns one extension, the repeats earn none."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "3")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "5")
    (tmp_path / "a.txt").write_text("hello")
    calls = {"n": 0}

    def fn(role, messages, **kw):
        calls["n"] += 1
        return 'ACTION: file_read\nARGS_JSON: {"path": "a.txt"}'

    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "read it"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    assert calls["n"] == 6                       # 3 steps + one 3-step extension
    assert "runaway safety cap" in _msgs(evs)


def test_unattended_runs_are_never_extended(tmp_path, monkeypatch):
    """text_doer / parallel_subtasks / the analysis fan-out pass no session_id
    and nobody is watching — they keep the old hard stop."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "2")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "5")
    fn, calls = _reader(tmp_path)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "long job"}], cwd=str(tmp_path),
        complete_fn=fn))                          # no session_id
    assert calls["n"] == 2
    assert "runaway safety cap" in _msgs(evs)


def test_caller_max_steps_is_never_extended(tmp_path, monkeypatch):
    """Chat Quick mode / the Doer pass an explicit budget — that is a
    deliberate choice, not a runaway guard, so it is honoured exactly."""
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "5")
    fn, calls = _reader(tmp_path)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "quick"}], cwd=str(tmp_path),
        complete_fn=fn, max_steps=2, session_id=_SID))
    assert calls["n"] == 2
    assert "runaway safety cap" in _msgs(evs)


# ── wall-clock deadline ─────────────────────────────────────────────────

def _clock(monkeypatch, step_s=4.0):
    """Monotonic clock that advances a fixed amount on every read, so the turn
    deadline trips after a few real steps instead of before the first one."""
    state = {"t": 1000.0}

    def fake():
        state["t"] += step_s
        return state["t"]
    monkeypatch.setattr(ca.time, "monotonic", fake)


def _clock_already_past(monkeypatch):
    """Deadline already blown at the FIRST loop-top check — no work done yet."""
    seq = iter([1000.0])

    def fake():
        try:
            return next(seq)
        except StopIteration:
            return 1_000_000.0
    monkeypatch.setattr(ca.time, "monotonic", fake)


def test_deadline_extends_a_progressing_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "10")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "2")
    _clock(monkeypatch)
    fn, calls = _reader(tmp_path)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "endless"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    assert "extended the turn by 10s" in _msgs(evs, "thought")
    assert "turn time budget" in _msgs(evs)
    # Bounded — 2 extensions, then the real stop; nowhere near a runaway.
    assert calls["n"] <= 12


def test_deadline_stop_banner_points_at_the_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "10")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "0")
    _clock_already_past(monkeypatch)
    fn, _calls = _churner()
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "endless"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    text = _msgs(evs)
    assert text.startswith("(stopped:")
    assert "Settings" in text and "AIFORGE_CHAT_TURN_DEADLINE_S" in text


# ── knob resolution ─────────────────────────────────────────────────────

def test_limits_default_env_and_store(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_CHAT_SAFETY_CAP", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    assert _limits._safety_cap() == 2000
    assert _limits._turn_deadline_s() == 3600.0
    assert _limits._cap_extensions() == 2

    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "50")
    assert _limits._safety_cap() == 50

    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"chat_safety_cap": 77})          # the Settings UI writes here
    assert _limits._safety_cap() == 77            # store beats env


def test_limits_clamp_a_bad_env_value(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "0")     # would stop every turn
    assert _limits._safety_cap() == 1
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "-3")
    assert _limits._cap_extensions() == 0
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "not-a-number")
    assert _limits._turn_deadline_s() == 3600.0


def test_deadline_no_extension_before_any_work(tmp_path, monkeypatch):
    """The deadline blows before the first tool ran: there is no progress to
    protect, so the extension budget is not spent on it."""
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "10")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "3")
    _clock_already_past(monkeypatch)
    fn, calls = _reader(tmp_path)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "endless"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    assert calls["n"] == 0
    assert "turn time budget" in _msgs(evs)


def test_deadline_accepts_a_fractional_env_value(monkeypatch, tmp_path):
    """The store is integer-only; routing the env var through it turned a
    perfectly ordinary '1800.5' into the 3600 default — silently doubling the
    guard on upgrade."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "1800.5")
    assert _limits._turn_deadline_s() == 1800.5
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "0.5")
    assert _limits._turn_deadline_s() == 0.5
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "0")
    assert _limits._turn_deadline_s() == 0.0        # still disables
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "999999")
    assert _limits._turn_deadline_s() == _limits._MAX_TURN_SECONDS

    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"chat_turn_deadline_s": 60})
    assert _limits._turn_deadline_s() == 60.0       # a UI value still wins


def test_extension_budget_bounds_the_product(monkeypatch, tmp_path):
    """Each settings field validates in isolation, so the multiplication is
    where an innocent pair becomes a multi-day turn."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "50")
    # 1M steps × 51 would be 51M steps; the step ceiling allows none.
    assert _limits._extension_budget(1_000_000, 0) == 0
    # 24h deadline × 51 would be ~51 days; likewise.
    assert _limits._extension_budget(2000, 86_400) == 0
    # A 1h deadline allows 23 extensions before the 24h ceiling, so the
    # operator's 50 is trimmed to that — the tighter of the two bounds wins.
    assert _limits._extension_budget(2000, 3600) == 23
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "2")
    assert _limits._extension_budget(2000, 3600) == 2      # under both ceilings
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "0")
    assert _limits._extension_budget(2000, 3600) == 0


def test_settings_unset_restores_the_env_override(monkeypatch, tmp_path):
    """Saving from the UI must not permanently kill the documented env var."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "50")
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"chat_safety_cap": 77})
    assert _limits._safety_cap() == 77
    rs.unset(["chat_safety_cap"])
    assert _limits._safety_cap() == 50               # env is live again
    monkeypatch.delenv("AIFORGE_CHAT_SAFETY_CAP")
    assert _limits._safety_cap() == 2000             # …then the default
