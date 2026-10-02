"""A stuck run restarts from a handoff (goal, done, files, what failed, the last
error, next step) in a fresh context instead of from its failed transcript; the
pipeline Doer is told what already failed."""
import pytest

from aiforge_core.runtime import context_offload, handoff
from aiforge_core.runtime.chat_agent._turn import _escalate as E
from aiforge_core.runtime.graph_pipeline import _gates as G


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CHAT_PAUSE_ON_STUCK", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_STUCK_ESCALATIONS", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_STUCK_RESTART", raising=False)


def _call(tool, **args):
    import json
    return {"role": "assistant", "content": f"ACTION: {tool}\nARGS_JSON: {json.dumps(args)}"}


def _obs(t):
    return {"role": "user", "content": "OBSERVATION: " + t}


class St:
    def __init__(self):
        self.convo = [{"role": "system", "content": "SYSTEM RULES"},
                      {"role": "user", "content": "make the read path under 500 ms"},
                      _call("run_command", cmd="python bench.py"),
                      _obs("running\nTraceback (most recent call last)\nValueError: bad shape"),
                      _call("run_command", cmd="python bench.py"),
                      _obs("running\nTraceback (most recent call last)\nValueError: bad shape")]
        self.role = "doer"
        self.goal = "make the read path under 500 ms"
        self.board = {"part-1": {"title": "profile it", "status": "done"},
                      "part-2": {"title": "add the index", "status": "pending"}}
        self.file_hashes = {"/w/src/read.py": "abc"}
        self.read_sigs_seen = {"x"}
        self.recent_outputs = []


def _escalate(st, why="You keep repeating `run_command`."):
    gen = E.escalate(st, why)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def test_the_failed_attempt_is_written_down():
    items = []
    handoff.note_failed(items, "run_command({'cmd': 'x'}) -> ValueError")
    handoff.note_failed(items, "run_command({'cmd': 'x'}) -> ValueError")
    assert len(items) == 1
    for i in range(15):
        handoff.note_failed(items, f"try {i}")
    assert len(items) == handoff.MAX_FAILED and items[-1] == "try 14"
    assert "ValueError: bad shape" in handoff.last_attempt(St().convo)


def test_the_handoff_has_goal_done_files_failed_error_next():
    st = St()
    st.failed_approaches = ["python bench.py -> ValueError: bad shape"]
    h = handoff.build_chat(st)
    text = handoff.render(h, "off:abc123")
    assert "GOAL: make the read path under 500 ms" in text
    assert "DONE (verified): profile it" in text
    assert "/w/src/read.py" in text
    assert "ALREADY TRIED AND FAILED" in text and "bad shape" in text
    assert "NEXT: add the index" in text
    assert 'memory_lookup {"id": "off:abc123"}' in text


def test_the_second_stuck_trip_restarts_with_a_fresh_context():
    st = St()
    assert _escalate(st) == "continue"                   # trip 1: nudge, no restart
    assert len(st.convo) >= 6
    assert _escalate(st) == "continue"                   # trip 2: restart
    roles = [m["role"] for m in st.convo]
    assert roles[0] == "system" and len(st.convo) == 2
    text = st.convo[1]["content"]
    assert "[HANDOFF" in text and "ALREADY TRIED AND FAILED" in text
    assert "Traceback" not in text                             # the transcript is gone
    assert "ValueError: bad shape" in text                     # the error line is kept
    assert "<<AIFORGE_TASK_BOARD>>" in st.convo[0]["content"]  # board pinned back
    assert "ORIGINAL TASK" in st.convo[0]["content"]           # goal pinned
    oid = text.split('"id": "')[1].split('"')[0]
    assert "Traceback" in context_offload.load(oid)["text"]    # lossless
    assert not st.read_sigs_seen and st.restarts == 1


def test_restart_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STUCK_RESTART", "0")
    st = St()
    _escalate(st)
    _escalate(st)
    assert len(st.convo) > 2


def test_pipeline_remembers_what_failed_for_the_next_doer_prompt():
    state = {"_iter_fail": ["AssertionError: x != y", 1, "tests/test_a.py::t"],
             "doer_iters": 1}
    G._same_failure_stop(state)
    assert "the tests failed" in state["failed_approaches_md"]
    G._no_edit_stop({**state, "_iter_edits": 0, "failed_approaches": []})
    from aiforge_core.runtime import text_doer_seed as S
    assert any(k == "failed_approaches_md" for k, _ in S._SEED_VARS)
    seed = S._unbudgeted_seed({"plan_md": "p", **state}, [])
    assert "ALREADY FAILED" in seed and "tests/test_a.py::t" in seed


def test_a_replan_keeps_the_ledger(monkeypatch):
    monkeypatch.setattr(G, "PLATEAU_REPLANS", 2)
    from types import SimpleNamespace
    ctx = SimpleNamespace(state={"feedback_verdict": "partial loop_budget_kill: same_failure",
                                 "loop_budget_reason": "same_failure",
                                 "failed_approaches_md": "- earlier", "failed_approaches": ["earlier"],
                                 "doer_iters": 4}, route=None)
    G._validator_gate(ctx)
    assert ctx.route == G.ROUTE_REPLAN
    assert "earlier" in ctx.state["failed_approaches_md"]
    assert "previous plan stalled" in ctx.state["failed_approaches_md"]
