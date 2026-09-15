"""Running aiforge twice in one folder lands in the same chat."""

from __future__ import annotations

from aiforge_cli import sessions


def test_a_folder_remembers_its_chat(tmp_path):
    path = tmp_path / "cli-sessions.json"
    sessions.remember(path, "/work/repo", 12)
    assert sessions.for_folder(path, "/work/repo", {12, 13}) == 12


def test_a_chat_deleted_elsewhere_is_not_resumed(tmp_path):
    path = tmp_path / "cli-sessions.json"
    sessions.remember(path, "/work/repo", 12)
    # The web UI deleted #12; resuming it would 404 on the first message.
    assert sessions.for_folder(path, "/work/repo", {13}) is None


def test_a_corrupt_map_is_survivable(tmp_path):
    path = tmp_path / "cli-sessions.json"
    path.write_text("{not json")
    assert sessions.load(path) == {}
    sessions.remember(path, "/work/repo", 1)
    assert sessions.load(path) == {"/work/repo": 1}


def test_forget_drops_every_folder_pointing_at_a_session(tmp_path):
    path = tmp_path / "cli-sessions.json"
    sessions.remember(path, "/a", 5)
    sessions.remember(path, "/b", 5)
    sessions.remember(path, "/c", 6)
    sessions.forget(path, 5)
    assert sessions.load(path) == {"/c": 6}
