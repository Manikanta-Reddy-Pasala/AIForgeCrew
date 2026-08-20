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


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """These tests drive the knobs through env vars, and a STORED value beats
    env. conftest only setdefaults AIFORGE_CONFIG_DIR, so a box that exports it
    (docker-compose does) would run them against a real store."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))


def _churner():
    """The RUNAWAY: novel tool args every step, but it reads nothing new and
    changes nothing. Distinct-action counting would call this "progress" — it
    is exactly what the deadline was written to stop."""
    calls = {"n": 0}

    def fn(role, messages, **kw):
        calls["n"] += 1
        return f'ACTION: run_command\nARGS_JSON: {{"cmd": "echo {calls["n"]}"}}'
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
    # The banner names the budget that ACTUALLY stopped it — Quick mode's own,
    # not a Settings cap that had no say (and may itself be 0 = no limit).
    assert "Quick mode" in _msgs(evs)
    assert "AIFORGE_CHAT_QUICK_STEPS" in _msgs(evs)


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
    # 0 is now a REAL value — "no step cap" — not a typo to clamp away. A
    # NEGATIVE one is still a typo, and clamping it toward 0 would disable the
    # guard on a `$((N-1))` underflow: fall back to the default instead.
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "0")
    assert _limits._safety_cap() == 0
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "-5")
    assert _limits._safety_cap() == 2000
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


def test_re_reads_after_a_condense_are_not_new_knowledge(tmp_path, monkeypatch):
    """The condense clears the duplicate-read memory (its results are gone), so
    the same three files look "new" on every extension. Counting progress off
    that set handed a pure re-read loop the whole budget."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "20")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "5")
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("x" * 4000)
    calls = {"n": 0}

    def fn(role, messages, **kw):
        calls["n"] += 1
        return ('ACTION: file_read\nARGS_JSON: '
                f'{{"path": "f{calls["n"] % 3}.txt"}}')

    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "read them forever"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    # 3 files = 3 new reads = ONE earned extension, not five.
    assert calls["n"] == 40                      # 20 + one 20-step extension
    assert len([e for e in evs if "extended the step budget"
                in e.get("text", "")]) == 1
    assert "runaway safety cap" in _msgs(evs)


def test_progress_counting_survives_stuck_recoveries_off(tmp_path, monkeypatch):
    """AIFORGE_CHAT_STUCK_RECOVERIES=0 turns off the duplicate-read NUDGE. It
    must not also delete half the progress signal — a read-only research turn
    on that box would never extend."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "3")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "2")
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_RECOVERIES", "0")
    fn, calls = _reader(tmp_path)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "research"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    assert calls["n"] == 9                        # 3 + two 3-step extensions
    assert "extended the step budget" in _msgs(evs, "thought")


def test_shell_work_that_changes_the_tree_counts_as_progress(tmp_path, monkeypatch):
    """`run_command` is neither a counted read nor a counted edit, so a turn
    doing its work through the shell (sed -i, a build, git apply) scored zero.
    The worktree fingerprint catches it."""
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "2")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "1")
    calls = {"n": 0}

    def fn(role, messages, **kw):
        calls["n"] += 1
        return ('ACTION: run_command\nARGS_JSON: '
                f'{{"cmd": "echo {calls["n"]} > out{calls["n"]}.txt"}}')

    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "write files via the shell"}],
        cwd=str(tmp_path), complete_fn=fn, session_id=_SID))
    assert "extended the step budget" in _msgs(evs, "thought")
    assert calls["n"] == 4                        # 2 + one 2-step extension


def test_both_guards_expiring_together_spend_one_extension(tmp_path, monkeypatch):
    """The cap and the deadline can expire on the same iteration. The second is
    the same moment, not a second failure — it must not hard-stop the turn with
    extensions still unspent."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "2")
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "20")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "3")
    # 8s per clock read: the 2-step cap and the 20s deadline both expire on the
    # third iteration.
    _clock(monkeypatch, step_s=8.0)
    fn, calls = _reader(tmp_path)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "work"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    thoughts = _msgs(evs, "thought")
    assert "extended the step budget" in thoughts
    # The deadline rode the SAME grant instead of hard-stopping the turn.
    assert "extended the turn" in thoughts
    assert "turn time budget" not in _msgs(evs) or calls["n"] > 2


def test_negative_deadline_does_not_disable_the_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "-1")
    assert _limits._turn_deadline_s() == 3600.0   # NOT 0 (= no deadline)


def test_settings_get_reports_a_float_env_var_as_its_integer_part(monkeypatch, tmp_path):
    """The card must not display 3600 for a box actually running 1800.5."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "1800.5")
    from aiforge_core.config import runtime_settings as rs
    assert rs.get("chat_turn_deadline_s") == 1800
    assert _limits._turn_deadline_s() == 1800.5   # the runtime keeps precision


def test_max_steps_zero_falls_back_to_the_default_cap(tmp_path, monkeypatch):
    """0 has always meant "unset"; it must not become a one-step turn."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "2")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "0")
    fn, calls = _reader(tmp_path)
    list(ca.run_chat_agent([{"role": "user", "content": "x"}],
                           cwd=str(tmp_path), complete_fn=fn, max_steps=0,
                           session_id=_SID))
    assert calls["n"] == 2


# ── no limits (cap 0) ───────────────────────────────────────────────────

def _answers_on(step: int, tmp_path):
    """Reads a new file each step, then finally ANSWERS on ``step``."""
    for i in range(step + 2):
        (tmp_path / f"g{i}.txt").write_text(f"contents {i}")
    calls = {"n": 0}

    def fn(role, messages, **kw):
        calls["n"] += 1
        if calls["n"] >= step:
            return "all done"
        return ('ACTION: file_read\nARGS_JSON: '
                f'{{"path": "g{calls["n"]}.txt"}}')
    return fn, calls


def test_step_cap_zero_means_no_cap(tmp_path, monkeypatch):
    """0 = no limit, the same convention the turn deadline already uses. The
    turn ends because the AGENT finished, not because a guard fired."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "0")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "0")
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "0")
    fn, calls = _answers_on(6, tmp_path)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "long job"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID))
    assert calls["n"] == 6
    assert "runaway safety cap" not in _msgs(evs)
    assert "extended the step budget" not in _msgs(evs, "thought")
    assert evs[-1] == {"type": "done"}

    # The control: the SAME agent under a finite cap of 3 is stopped at 3. That
    # is what makes the assertion above mean "no cap" rather than "some cap
    # this agent happened not to reach".
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "3")
    fn2, calls2 = _answers_on(6, tmp_path)
    evs2 = list(ca.run_chat_agent(
        [{"role": "user", "content": "long job"}], cwd=str(tmp_path),
        complete_fn=fn2, session_id=_SID))
    assert calls2["n"] == 3
    assert "runaway safety cap" in _msgs(evs2)


def test_quick_mode_still_wins_over_no_limits(tmp_path, monkeypatch):
    """`max_steps` is a per-message choice (Quick mode). "No limits" is the
    operator default — it must not silently un-cap a turn the caller capped."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "0")
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "0")
    fn, calls = _churner()
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "spin"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=_SID, max_steps=3))
    assert calls["n"] == 3
    assert "Quick mode" in _msgs(evs)


def test_zero_cap_is_storable(monkeypatch, tmp_path):
    """Store level. The ROUTE is the layer that actually rejected 0 — see
    tests/api/test_agent_limits_no_cap.py, which is where the regression was."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg2"))
    from aiforge_core.config import _filecache, runtime_settings as rs
    _filecache.clear()
    rs.set_many({"chat_safety_cap": 0})
    assert rs.get("chat_safety_cap") == 0
    assert _limits._safety_cap() == 0


def test_an_unattended_run_keeps_its_cap(tmp_path, monkeypatch):
    """"No limits" is a promise to someone who can press Stop. The cancel check
    is gated on a session id, so a run without one (jobs scheduler, analysis
    fan-out, subtask runners) would have NO brake at all."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "0")
    monkeypatch.setenv("AIFORGE_CHAT_UNATTENDED_CAP", "3")
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "0")
    fn, calls = _churner()
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "spin"}], cwd=str(tmp_path),
        complete_fn=fn, session_id=None))
    assert calls["n"] == 3
    # …and it points at the knob that stopped it. Saying "raise the step cap"
    # here would send the operator to a setting they had already zeroed.
    assert "nobody watching" in _msgs(evs)
    assert "AIFORGE_CHAT_UNATTENDED_CAP" in _msgs(evs)


def test_no_step_cap_still_lets_the_deadline_extend(monkeypatch, tmp_path):
    """Turning the STEP cap off must not make a turn stop SOONER.

    Gating both axes on the step cap did exactly that: with cap 0 the turn kept
    its 1h deadline but lost every extension of it, so asking for fewer limits
    produced a harsher stop than the default. The absolute 24h ceiling is
    enforced by the _MAX_TURN_SECONDS trim, which needs no help from the step
    cap."""
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "2")
    assert _limits._extension_budget(0, 3600) == 2      # deadline can still grow
    assert _limits._extension_budget(2000, 3600) == 2
    monkeypatch.setenv("AIFORGE_CHAT_CAP_EXTENSIONS", "0")
    assert _limits._extension_budget(0, 3600) == 0      # operator said never


def test_an_unattended_cap_of_zero_is_a_typo_not_a_request(monkeypatch):
    """0 means "no cap" everywhere else — but a background run has no Stop
    button, so 0 here would be a fleet of unstoppable jobs. The first cut
    clamped it to 1 instead, which turned every scheduled job into a one-step
    turn."""
    monkeypatch.setenv("AIFORGE_CHAT_UNATTENDED_CAP", "0")
    assert _limits._unattended_cap() == 2000
    monkeypatch.setenv("AIFORGE_CHAT_UNATTENDED_CAP", "-5")
    assert _limits._unattended_cap() == 2000
    monkeypatch.setenv("AIFORGE_CHAT_UNATTENDED_CAP", "40")
    assert _limits._unattended_cap() == 40


def test_a_fractional_negative_cap_does_not_disable_the_guard(monkeypatch):
    """int(float(x)) truncates TOWARD ZERO, so -0.5 reached the sign check as
    0 — the one value that means "no cap". A unit file computing the cap with
    an expression that underflows must not silently remove the guard."""
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "-0.5")
    assert _limits._safety_cap() == 2000
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "-0.9")
    assert _limits._safety_cap() == 2000
    monkeypatch.setenv("AIFORGE_CHAT_SAFETY_CAP", "0.0")
    assert _limits._safety_cap() == 0        # an explicit 0 still means 0
