"""Core mode (runtime/chat_agent/core.py): opt-in profile that turns off the
layers that talk to the model between its steps, keeping panel, safety and
deterministic finish checks."""
from __future__ import annotations

import pathlib

from aiforge_core.runtime.chat_agent import core

ROOT = pathlib.Path(core.__file__).resolve().parents[2]       # aiforge_core/


def _clear(monkeypatch):
    for k in list(core.CORE_OFF) + ["AIFORGE_CHAT_CORE"]:
        monkeypatch.delenv(k, raising=False)


def test_off_by_default(monkeypatch):
    _clear(monkeypatch)
    core.apply()
    import os
    assert not any(k in os.environ for k in core.CORE_OFF)


def test_on_sets_every_switch_and_an_explicit_value_wins(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("AIFORGE_CHAT_CORE", "1")
    monkeypatch.setenv("AIFORGE_CHAT_PROBE_GUARD", "1")
    core.apply()
    import os
    assert os.environ["AIFORGE_CHAT_PROBE_GUARD"] == "1"
    for k, v in core.CORE_OFF.items():
        if k != "AIFORGE_CHAT_PROBE_GUARD":
            assert os.environ[k] == v


def test_every_switch_is_read_somewhere_else_in_the_code():
    src = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                    for p in ROOT.rglob("*.py") if p.name != "core.py")
    missing = [k for k in core.CORE_OFF if k not in src
               and k.replace("AIFORGE_CHAT_", "").lower() not in src]
    assert not missing


def test_panel_and_safety_layers_are_not_touched():
    kept = ("AIFORGE_CHAT_LLM_PLAN", "AIFORGE_CHAT_VERIFY_ON_FINAL", "AIFORGE_CHAT_SPEC_RUN",
            "AIFORGE_CHAT_DONE_WATCH", "AIFORGE_CHAT_DISCARD_GUARD", "AIFORGE_CHAT_REWRITE_GUARD",
            "AIFORGE_CHAT_STEER_REPLY", "AIFORGE_CHAT_PROACTIVE_RECALL", "AIFORGE_CHAT_TARGETS",
            "AIFORGE_CHAT_SAFETY_CAP", "AIFORGE_CHAT_TURN_DEADLINE_S")
    assert not set(kept) & set(core.CORE_OFF)


def test_off_switches_silence_their_layers(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("AIFORGE_CHAT_CORE", "1")
    core.apply()
    from types import SimpleNamespace
    from aiforge_core.runtime.chat_agent._turn import _probe_guard, _timebox
    assert _probe_guard.note(SimpleNamespace(), "run_command", {"cmd": "python -c 1"}) == ""
    assert _timebox.check(SimpleNamespace(board={})) is None
    from aiforge_core.runtime.stuck_policy import Policy
    assert Policy.load().stuck_restart is False


def test_a_stuck_trip_in_core_mode_keeps_the_history(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.delenv("AIFORGE_CHAT_STUCK_CONDENSE", raising=False)
    monkeypatch.setenv("AIFORGE_CHAT_CORE", "1")
    core.apply()
    import collections
    import types
    from aiforge_core.runtime.chat_agent._turn import _escalate as E
    from aiforge_core.runtime.chat_agent._turn._progress import progress_fields
    called = []
    monkeypatch.setattr(E, "_condense", lambda st: called.append(1))
    st = types.SimpleNamespace(
        convo=[{"role": "system", "content": "s"}, {"role": "user", "content": "OBSERVATION: x"}],
        role="doer", action_counts=collections.OrderedDict(), read_sigs_seen=set(),
        recent_outputs=collections.deque(maxlen=3), continue_nudges=0, stuck_recoveries=0,
        board={}, cwd="", session_id=None, **progress_fields())
    for _ in range(3):
        list(E.escalate(st, "You keep repeating `x` without progress."))
    assert not called
    assert "[loop guard — not the user]" in st.convo[-1]["content"]
