"""The model's own words for a shell call travel with the step, not the args."""
from __future__ import annotations

from aiforge_core.runtime.chat_agent._prompt import _parse
from aiforge_core.runtime.chat_agent._tools._schemas import CATALOG


def test_description_taken_off_run_command_args():
    step = _parse('ACTION: run_command\nARGS_JSON: {"cmd": "pytest -q", '
                  '"description": "Running  the coupon\\ntests"}')
    assert step["kind"] == "action"
    assert step["args"] == {"cmd": "pytest -q"}          # guards see the plain call
    assert step["description"] == "Running the coupon tests"


def test_no_description_is_fine():
    step = _parse('ACTION: run_command\nARGS_JSON: {"cmd": "ls"}')
    assert step["args"] == {"cmd": "ls"}
    assert "description" not in step


def test_other_tools_keep_their_description_argument():
    # jira_create's description is the issue body: an input, not a label.
    step = _parse('ACTION: jira_create\nARGS_JSON: {"project": "P", "summary": "s", '
                  '"description": "Body of the issue"}')
    assert step["args"]["description"] == "Body of the issue"
    assert "description" not in step


def test_odd_description_values_are_ignored_and_capped():
    step = _parse('ACTION: run_command\nARGS_JSON: {"cmd": "ls", "description": 42}')
    assert step["args"] == {"cmd": "ls"} and "description" not in step
    long = "x" * 500
    step = _parse('ACTION: run_command\nARGS_JSON: {"cmd": "ls", "description": "' + long + '"}')
    assert len(step["description"]) == 160


def test_schema_offers_description_for_run_command():
    _desc, props, required = CATALOG["run_command"]
    assert "description" in props and "description" not in required


def test_inline_args_rescue_still_works_with_a_description_only_marker():
    # `ACTION: run_command {"cmd": ...}` then a marker holding only a description.
    step = _parse('ACTION: run_command {"cmd": "pytest -q"}\n'
                  'ARGS_JSON: {"description": "Running the tests"}')
    assert step["args"] == {"cmd": "pytest -q"}
    assert step["description"] == "Running the tests"


def test_description_added_to_another_tool_is_taken_off_too():
    step = _parse('ACTION: file_read\nARGS_JSON: {"path": "a.py", "description": "Reading a.py"}')
    assert step["args"] == {"path": "a.py"}
    assert step["description"] == "Reading a.py"


def test_run_chat_agent_puts_description_and_secs_on_the_tool_events(monkeypatch, tmp_path):
    from aiforge_core.runtime.chat_agent import _loop

    def fake_inner(st, *a, **k):
        st.step_said = ("run_command", "Running the tests")
        yield {"type": "tool_start", "name": "run_command", "args": {"cmd": "pytest"}, "call_id": 1}
        yield {"type": "tool", "name": "run_command", "args": {"cmd": "pytest"}, "result": {"ok": True}, "call_id": 1}
        yield {"type": "tool", "name": "file_read", "args": {"path": "a"}, "result": {"ok": True}, "call_id": 2}

    class _St:
        complete_fn = None

    monkeypatch.setattr(_loop, "_build_loop_state", lambda *a, **k: _St())
    monkeypatch.setattr(_loop, "_drive", fake_inner)
    evs = list(_loop.run_chat_agent([{"role": "user", "content": "go"}], cwd=str(tmp_path)))
    start, done, other = evs
    assert start["description"] == done["description"] == "Running the tests"
    assert isinstance(done.get("secs"), float)
    assert "description" not in other and "secs" not in other
