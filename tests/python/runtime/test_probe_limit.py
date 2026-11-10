"""Throwaway python probes past the turn's limit are refused until the
tests run again (tomlkit: 97 of 261 calls were `python -c` probes)."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _probe_guard as P

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PROBE_BLOCK", "1")


PROBE = {"cmd": "cd /r && python3 -c \"import tomlkit; print(tomlkit.parse('a = 1'))\""}
TEST = {"cmd": "cd /r && python -m pytest tests -q"}


def _st(**kw):
    base = dict(edits_made=3, probe_edits_seen=3, native_on=True, cwd="/r", session_id=1)
    base.update(kw)
    return SimpleNamespace(**base)


def _probe(st):
    """One probe call: refused, or run (then counted by note())."""
    res = P.probe_refusal(st, "run_command", PROBE)
    if res is None:
        P.note(st, "run_command", PROBE)
    return res


def test_probes_run_until_the_limit_then_are_refused():
    st = _st()
    results = [_probe(st) for _ in range(P.PROBE_BLOCK_AT)]
    assert all(r is None for r in results)
    res = _probe(st)
    assert res and res["error"] == "probe_limit" and "tests" in res["hint"]
    assert _probe(st)["error"] == "probe_limit"          # still refused, not counted
    assert st.probe_count == P.PROBE_BLOCK_AT


def test_a_test_run_frees_a_few_probes():
    st = _st()
    for _ in range(P.PROBE_BLOCK_AT):
        _probe(st)
    assert _probe(st)
    P.note(st, "run_command", TEST)
    assert _probe(st) is None and _probe(st) is None     # PROBES_AFTER_TEST = 2
    assert _probe(st)["error"] == "probe_limit"


def test_never_in_turns_without_edits_or_read_only_or_for_other_commands():
    for st in (_st(edits_made=0), _st(readonly_mode=True), _st(plan_mode=True),
               _st(analyze_mode=True), _st(native_on=False)):
        st.probe_count = 99
        assert P.probe_refusal(st, "run_command", PROBE) is None
    st = _st(probe_count=99)
    assert P.probe_refusal(st, "run_command", TEST) is None
    assert P.probe_refusal(st, "run_command", {"cmd": "python app.py --port 80"}) is None
    assert P.probe_refusal(st, "read_lines", PROBE) is None


def test_off_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PROBE_BLOCK", "0")
    assert P.probe_refusal(_st(probe_count=99), "run_command", PROBE) is None


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_PROBE_BLOCK", raising=False)
    assert P.probe_refusal(_st(probe_count=99), "run_command", PROBE) is None
