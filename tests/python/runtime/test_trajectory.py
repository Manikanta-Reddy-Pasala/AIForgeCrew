from __future__ import annotations

import json

from aiforge_core.runtime import trajectory


def test_dump_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_TRAJECTORY_DIR", str(tmp_path))
    events = [
        {"type": "user_message", "text": "fix bug"},
        {"type": "tool_call", "tool_name": "editor",
         "tool_args": {"command": "view", "path": "x.py"}},
        {"type": "tool_response", "tool_result": {"ok": True}},
    ]
    state = {"ticket_identifier": "ONE-200"}
    out = trajectory.dump_trajectory("ONE-200", "abc123", events, state)
    assert out["ok"]
    assert out["n_events"] == 3

    loaded = trajectory.load_trajectory(out["path"])
    assert loaded["ok"]
    t = loaded["trajectory"]
    assert t["ticket_id"] == "ONE-200"
    assert t["run_id"] == "abc123"
    assert len(t["events"]) == 3
    assert t["events"][1]["tool_name"] == "editor"
    assert t["state"]["ticket_identifier"] == "ONE-200"


def test_dump_missing_run_id():
    out = trajectory.dump_trajectory("ONE-1", "", [])
    assert out["ok"] is False
    assert out["error"] == "missing_run_id"


def test_load_missing_file():
    out = trajectory.load_trajectory("/tmp/does/not/exist.json")
    assert out["ok"] is False
    assert out["error"] == "not_found"


def test_load_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    out = trajectory.load_trajectory(bad)
    assert out["ok"] is False
    assert out["error"] == "invalid_json"


def test_list_trajectories(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_TRAJECTORY_DIR", str(tmp_path))
    trajectory.dump_trajectory("ONE-1", "a", [])
    trajectory.dump_trajectory("ONE-1", "b", [])
    trajectory.dump_trajectory("ONE-2", "c", [])
    all_paths = trajectory.list_trajectories()
    assert len(all_paths) == 3
    one_only = trajectory.list_trajectories("ONE-1")
    assert len(one_only) == 2


def test_event_to_dict_coerces_object():
    class _Evt:
        type = "tool_call"
        tool_name = "bash"
        tool_args = {"command": "ls"}
    d = trajectory._event_to_dict(_Evt())
    assert d["type"] == "tool_call"
    assert d["tool_name"] == "bash"
    assert d["tool_args"] == {"command": "ls"}


def test_event_to_dict_reads_real_adk_event_shape():
    """ADK ``Event`` exposes content/parts/author, not type/text/kind.

    Pre-fix the dumper returned ``{}`` for every real event because it
    only looked at the legacy attribute names.
    """
    class _Part:
        text = "hello world"
        function_call = None
        function_response = None

    class _ToolCallPart:
        text = None
        class function_call:
            name = "bash"
            args = {"command": "ls"}
        function_response = None

    class _Content:
        role = "model"
        parts = [_Part(), _ToolCallPart()]

    class _Actions:
        state_delta = {"k": "v"}
        artifact_delta = None
        transfer_to_agent = None
        escalate = False

    class _AdkEvent:
        id = "evt-1"
        invocation_id = "inv-1"
        author = "doer"
        timestamp = 123.45
        partial = False
        branch = None
        long_running_tool_ids = None
        content = _Content()
        actions = _Actions()
        error_code = None
        error_message = None

    d = trajectory._event_to_dict(_AdkEvent())
    assert d["id"] == "evt-1"
    assert d["author"] == "doer"
    assert d["role"] == "model"
    assert d["parts"][0]["text"] == "hello world"
    assert d["parts"][1]["function_call"]["name"] == "bash"
    assert d["parts"][1]["function_call"]["args"] == {"command": "ls"}
    assert d["state_delta"] == {"k": "v"}
