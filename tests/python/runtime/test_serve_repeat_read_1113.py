"""The first repeat of a read is served, not refused (11-13: about half the
refusals were followed by another refusal of the same read)."""
from __future__ import annotations

import collections
import types

from aiforge_core.runtime.chat_agent._turn import _action as A
from aiforge_core.runtime.chat_agent._turn._progress import progress_fields


def _st():
    return types.SimpleNamespace(
        convo=[{"role": "system", "content": "s"},
               {"role": "user", "content": "OBSERVATION: x"}],
        role="doer", action_counts=collections.OrderedDict(),
        read_sigs_seen=set(), read_windows={}, recent_outputs=collections.deque(maxlen=3),
        continue_nudges=0, stuck_recoveries=0, board={}, cwd="", session_id=None,
        edits_made=0, **progress_fields())


def _run(st, args, sig):
    gen = A._action_stall_guard(st, "read_lines", args, sig, True)
    ev = []
    try:
        while True:
            ev.append(next(gen))
    except StopIteration as stop:
        return ev, stop.value


def _shown(st, sig, path, a, b):
    st.read_sigs_seen.add(sig)
    st.read_windows[sig] = (path, a, b)


def test_first_repeat_is_served_in_full_second_is_skipped():
    st = _st()
    _shown(st, "s1", "core.py", 100, 200)
    args = {"path": "core.py", "start": 100, "end": 200}
    assert _run(st, args, "s1") == ([], None)
    assert st.serve_repeat_now is True            # the read dedupe is bypassed
    ev, r = _run(st, args, "s1")
    assert r == "continue" and "duplicate read skipped" in ev[0]["text"]
    assert st.serve_repeat_now is False


def test_a_shifted_range_of_the_same_file_counts_as_the_same_repeat():
    st = _st()
    _shown(st, "s1", "core.py", 100, 200)
    assert _run(st, {"path": "core.py", "start": 101, "end": 199}, "s2")[1] is None
    assert _run(st, {"path": "core.py", "start": 102, "end": 198}, "s3")[1] == "continue"


def test_an_edit_allows_one_more_served_repeat():
    st = _st()
    _shown(st, "s1", "core.py", 100, 200)
    args = {"path": "core.py", "start": 100, "end": 200}
    assert _run(st, args, "s1")[1] is None
    st.edits_made = 1
    assert _run(st, args, "s1")[1] is None


def test_off_switch_restores_the_skip(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SERVE_REPEAT_READ", "0")
    st = _st()
    _shown(st, "s1", "core.py", 100, 200)
    assert _run(st, {"path": "core.py", "start": 100, "end": 200}, "s1")[1] == "continue"
