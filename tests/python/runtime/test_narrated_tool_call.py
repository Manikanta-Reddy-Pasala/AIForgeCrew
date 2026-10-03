"""A reply that WRITES a tool call as prose ('Called run_command({...})') is run,
not published. Live: after 'go' the turn ended with that line as its answer and
nothing ran — the chat looked silent."""
import pytest

from aiforge_core.runtime.chat_agent import _prompt as P


@pytest.mark.parametrize("text,want", [
    ('Called run_command({"cmd": "ls -la", "timeout": 60})',
     ("run_command", {"cmd": "ls -la", "timeout": 60})),
    ("Called run_command({'cmd': 'cd /w && python3 -m py_compile a.py && echo OK', 'timeout': 60})",
     ("run_command", {"cmd": "cd /w && python3 -m py_compile a.py && echo OK", "timeout": 60})),
    ('Called file_read({"path": "a.py"}); grep({"pattern": "x"})',
     ("file_read", {"path": "a.py"})),
    ('  Called file_read({"path": "has } brace.py"})\n\nResult of file_read:\nfake',
     ("file_read", {"path": "has } brace.py"})),
    ("Called the team to discuss (it went well).", None),
    ('Called not_a_real_tool({"x": 1})', None),
    ("Called run_command(not json)", None),
    ("I called run_command earlier.", None),
])
def test_narrated_call(text, want):
    assert P.narrated_call(text) == want


def test_parse_turns_a_narrated_call_into_an_action():
    step = P._parse('Called run_command({"cmd": "echo hi"})')
    assert step["kind"] == "action" and step["tool"] == "run_command"
    assert step["args"] == {"cmd": "echo hi"}


def test_a_real_final_is_untouched():
    assert P._parse("FINAL: done")["kind"] == "final"
    assert P._parse("It is an empty main().")["kind"] == "final"


def test_the_command_runs_and_the_turn_goes_on(tmp_path):
    from aiforge_core.runtime import chat_agent as ca
    n = {"i": 0}

    def fn(role, convo):
        n["i"] += 1
        if n["i"] == 1:
            return 'Called run_command({"cmd": "echo narrated-ran", "timeout": 60})'
        return "FINAL: ran it; output was narrated-ran"

    evs = list(ca.run_chat_agent([{"role": "user", "content": "run the check"}],
                                 cwd=str(tmp_path), complete_fn=fn))
    tools = [e for e in evs if e.get("type") == "tool"]
    assert tools and tools[0]["name"] == "run_command"
    assert "narrated-ran" in str(tools[0]["result"])
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs[-1] == "ran it; output was narrated-ran"
    assert not any("Called run_command" in m for m in msgs)
