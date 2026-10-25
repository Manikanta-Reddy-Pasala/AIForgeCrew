# tests/python/runtime/test_log_retention.py
"""Logs and evidence older than the retention window are cleaned up."""
import json
import os
import sqlite3
import time

import pytest

from aiforge_core.runtime import log_retention as R

DAY = 86400


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(R, "days", lambda: 15)
    return tmp_path


def _old(p, age_days):
    t = time.time() - age_days * DAY
    os.utime(p, (t, t))


def test_old_files_in_log_folders_go_recent_ones_stay(root):
    (root / "chat_traces").mkdir()
    (root / "trajectories" / "T-1").mkdir(parents=True)
    old = root / "chat_traces" / "old.md"
    new = root / "chat_traces" / "new.md"
    traj = root / "trajectories" / "T-1" / "run.json"
    for p in (old, new, traj):
        p.write_text("x" * 100)
    _old(old, 20)
    _old(traj, 30)
    _old(new, 3)
    out = R.sweep_files(time.time() - 15 * DAY, root)
    assert not old.exists() and not traj.exists() and new.exists()
    assert not (root / "trajectories" / "T-1").exists()     # empty folder removed
    assert out["files_deleted"] == 2


def test_app_logs_are_emptied_or_cut_never_deleted(root, monkeypatch):
    monkeypatch.setenv("AIFORGE_LOG_MAX_MB", "1")
    (root / "logs").mkdir()
    idle = root / "logs" / "orchestrator-old.ndjson"
    idle.write_text('{"ts": "x"}\n' * 100)
    _old(idle, 20)
    big = root / "logs" / "orchestrator-doer.ndjson"
    big.write_bytes(b"".join(b"line %07d\n" % i for i in range(200_000)))   # ~2.6 MB
    out = R.sweep_files(time.time() - 15 * DAY, root)
    assert idle.exists() and idle.stat().st_size == 0
    data = big.read_bytes()
    assert len(data) <= 512 * 1024 and data.endswith(b"line 0199999\n")
    assert data.startswith(b"line ")                 # cut at a line boundary
    assert out["files_trimmed"] == 2 and out["files_deleted"] == 0


def test_an_append_handle_keeps_writing_after_a_cut(root, monkeypatch):
    monkeypatch.setenv("AIFORGE_LOG_MAX_MB", "1")
    (root / "logs").mkdir()
    p = root / "logs" / "orchestrator-chat.ndjson"
    with open(p, "ab") as fh:
        fh.write(b"x" * 2_000_000 + b"\n")
        fh.flush()
        R.sweep_files(time.time() - 15 * DAY, root)
        fh.write(b"after\n")
    assert p.read_bytes().endswith(b"after\n") and b"\0" not in p.read_bytes()


def test_other_files_in_the_log_folder_are_only_deleted_when_idle(root, monkeypatch):
    monkeypatch.setenv("AIFORGE_LOG_MAX_MB", "1")
    (root / "logs").mkdir()
    idle = root / "logs" / "graph-runner.err"
    idle.write_text("old")
    _old(idle, 20)
    busy = root / "logs" / "graphify-all.log"
    busy.write_bytes(b"x" * 2_000_000)            # big but not ours: never cut
    target = root / "elsewhere.ndjson"
    target.write_bytes(b"y" * 2_000_000)
    (root / "logs" / "orchestrator-link.ndjson").symlink_to(target)
    R.sweep_files(time.time() - 15 * DAY, root)
    assert not idle.exists()
    assert busy.stat().st_size == 2_000_000 and target.stat().st_size == 2_000_000


def test_a_replaced_database_starts_the_mark_over(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "days", lambda: 15)
    path = str(tmp_path / "chat.db")
    con = _db(path)
    old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 20 * DAY))
    con.execute("INSERT INTO chat_messages VALUES (1, 1, 'assistant', 'a', ?, ?)",
                (json.dumps([{"type": "turn_evidence", "evidence": {}}]), old))
    con.commit()
    con.close()
    R._save_mark(path, 999)
    assert R.sweep_chat_db(time.time() - 15 * DAY, path)["messages_slimmed"] == 1


def test_top_level_files_of_other_programs_are_left_alone(root):
    p = root / "scheduler.log"
    p.write_bytes(b"x" * 100)
    _old(p, 99)
    R.sweep_files(time.time() - 15 * DAY, root)
    assert p.read_bytes() == b"x" * 100


def test_user_project_files_are_never_touched(root):
    proj = root / "chat-workspaces" / "session-1"
    proj.mkdir(parents=True)
    f = proj / "server.log"
    f.write_text("mine")
    _old(f, 99)
    R.sweep_files(time.time() - 15 * DAY, root)
    assert f.exists()


def _db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, session_id INT, "
                "role TEXT, content TEXT, steps TEXT NOT NULL DEFAULT '[]', "
                "created_at TEXT)")
    return con


def test_old_turns_lose_evidence_and_bulky_output_but_keep_text(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "days", lambda: 15)
    path = str(tmp_path / "chat.db")
    con = _db(path)
    steps = [{"type": "subtasks", "items": [{"slug": "a", "status": "done",
                                             "evidence": {"rows": [1] * 50}}]},
             {"type": "turn_evidence", "evidence": {"rows": [2] * 50}},
             {"type": "tool", "name": "run_command",
              "result": {"ok": True, "stdout": "o" * 50_000}}]
    old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 20 * DAY))
    new = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - DAY))
    con.execute("INSERT INTO chat_messages VALUES (1, 1, 'assistant', 'old answer', ?, ?)",
                (json.dumps(steps), old))
    con.execute("INSERT INTO chat_messages VALUES (2, 1, 'assistant', 'new answer', ?, ?)",
                (json.dumps(steps), new))
    con.commit()
    con.close()
    out = R.sweep_chat_db(time.time() - 15 * DAY, path)
    assert out["messages_slimmed"] == 1
    again = R.sweep_chat_db(time.time() - 15 * DAY, path)
    assert again["messages_slimmed"] == 0               # each message once
    con = sqlite3.connect(path)
    rows = dict(con.execute("SELECT id, steps FROM chat_messages").fetchall())
    texts = dict(con.execute("SELECT id, content FROM chat_messages").fetchall())
    s1 = json.loads(rows[1])
    assert texts[1] == "old answer"
    assert [s["type"] for s in s1] == ["subtasks", "tool"]
    assert "evidence" not in s1[0]["items"][0] and s1[0]["items"][0]["status"] == "done"
    assert len(s1[1]["result"]["stdout"]) < 3000
    assert json.loads(rows[2]) == steps                     # a recent turn is untouched


def test_zero_days_keeps_everything(monkeypatch):
    monkeypatch.setattr(R, "days", lambda: 0)
    assert R.sweep() == {}


def test_default_is_fifteen_days(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_LOG_RETENTION_DAYS", raising=False)
    from aiforge_core.config import runtime_settings
    assert runtime_settings.get("log_retention_days") == 15
