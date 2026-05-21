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
