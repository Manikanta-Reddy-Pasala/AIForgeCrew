import json
import pytest
from aiforge_core.runtime import chat_trace


@pytest.fixture
def trace_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TRACE_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_TRACE", "1")
    return tmp_path


def test_append_turn_writes_md_and_jsonl(trace_dir):
    p = chat_trace.append_turn(
        session_id=3, prompt="do a thing",
        steps=[{"type": "tool", "name": "grep_repo", "args": {"q": "x"},
                "result": {"ok": True}},
               {"type": "thought", "text": "thinking"},
               {"type": "error", "text": "boom"}],
        final_text="done the thing", team=True, cwd="/repo")
    assert p
    assert p.endswith("session_3.md")
    md = (trace_dir / "session_3.md").read_text()
    assert "**User:** do a thing" in md
    assert "grep_repo" in md
    assert "💭" in md
    assert "ERROR: boom" in md
    assert "**Response:** done the thing" in md
    assert "· team" in md
    # jsonl sibling holds the structured turn
    rec = json.loads((trace_dir / "session_3.jsonl").read_text().strip())
    assert rec["session_id"] == 3
    assert rec["n_tools"] == 1
    assert rec["mode"] == "team"
    assert rec["response"] == "done the thing"


def test_second_turn_appends(trace_dir):
    for i in range(2):
        chat_trace.append_turn(session_id=5, prompt=f"msg{i}", steps=[],
                               final_text=f"reply{i}", team=False)
    lines = (trace_dir / "session_5.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2


def test_disabled_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TRACE_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_TRACE", "0")
    assert chat_trace.append_turn(session_id=1, prompt="x", steps=[],
                                  final_text="y", team=False) is None
    assert not list(tmp_path.glob("session_*"))
