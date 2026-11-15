"""Where a project's chats work: the saved choice, the env that wins over it,
reading the user's reply, and the chat's pending state on disk."""
from __future__ import annotations

import json
import os

import pytest

from tests.python.runtime.worktree_ask_fixtures import wenv  # noqa: F401


def _wp():
    from aiforge_core.runtime import worktree_prefs
    return worktree_prefs


def test_set_get_clear_round_trip_in_the_config_file(wenv):
    wp = _wp()
    assert wp.get(str(wenv.repo)) is None and wp.place_for(str(wenv.repo)) is None
    assert wp.needs_ask(str(wenv.repo))
    row = wp.set_place(str(wenv.repo), "config")
    assert row["place"] == "config" and row["set_at"]
    data = json.loads(open(wenv.cfg / "worktree_prefs.json").read())
    assert data == {wenv.real: row}                       # keyed by the real path
    assert wp.place_for(str(wenv.repo) + "/") == "config"
    assert not wp.needs_ask(str(wenv.repo))
    assert wp.clear(str(wenv.repo)) and wp.get(str(wenv.repo)) is None
    assert not wp.clear(str(wenv.repo))
    with pytest.raises(ValueError):
        wp.set_place(str(wenv.repo), "elsewhere")


def test_an_explicit_env_wins_and_nothing_is_asked(wenv, monkeypatch):
    wp = _wp()
    wp.set_place(str(wenv.repo), "none")
    monkeypatch.setenv("AIFORGE_CHAT_WORKTREE_PLACE", "config")
    assert wp.place_for(str(wenv.repo)) == "config" and not wp.needs_ask("/x")
    monkeypatch.setenv("AIFORGE_CHAT_WORKTREE_PLACE", "repo")
    assert wp.place_for("/anything") == "repo"
    monkeypatch.delenv("AIFORGE_CHAT_WORKTREE_PLACE")
    monkeypatch.setenv("AIFORGE_CHAT_WORKTREES", "0")
    assert wp.place_for("/anything") == "none" and not wp.needs_ask("/anything")
    monkeypatch.delenv("AIFORGE_CHAT_WORKTREES")
    assert wp.place_for(str(wenv.repo)) == "none"        # back to the saved one
    monkeypatch.setenv("AIFORGE_CHAT_WORKTREE_ASK", "0")
    assert not wp.needs_ask("/new/project")


def test_a_broken_prefs_file_reads_as_no_setting(wenv):
    wp = _wp()
    os.makedirs(wenv.cfg, exist_ok=True)
    (wenv.cfg / "worktree_prefs.json").write_text("{not json")
    assert wp.get(str(wenv.repo)) is None and wp.all_prefs() == {}
    wp.set_place(str(wenv.repo), "repo")                  # and it heals on write
    assert wp.get(str(wenv.repo))["place"] == "repo"


@pytest.mark.parametrize("text,want", [
    ("1", "repo"), ("2", "config"), ("3", "none"), (" 2) ", "config"), ("option 3", "none"),
    ("#1 please", "repo"), ("inside the project", "repo"), ("repo", "repo"),
    ("in AIForge's folder", "config"), ("home folder", "config"), ("config", "config"),
    ("no worktree", "none"), ("in place", "none"), ("none", "none"),
    ("edit the project folder directly", "none"),
])
def test_choices_are_read(text, want):
    assert _wp().parse_choice(text) == want


@pytest.mark.parametrize("text", [
    "", "fix the failing test in src/cart.py", "12", "4",
    "please refactor the repo so the project builds with the new compiler flags",
    "what is this?",
])
def test_anything_else_is_not_a_choice(text):
    assert _wp().parse_choice(text) is None


def test_the_question_names_the_exact_paths(wenv):
    wp = _wp()
    from aiforge_core.runtime import chat_worktree
    holder = chat_worktree.config_holder(wenv.real)
    q = wp.question(wenv.real, holder)
    assert f"This project ({wenv.real}) has no worktree setting yet." in q
    assert f"1) Own worktree inside the project: {wenv.real}/.aiforge-worktrees/ (git-ignored)" in q
    assert f"2) Own worktree in AIForge's folder: {holder}/" in q
    assert holder.startswith(str(wenv.cfg / "chat-worktrees" / "shop-"))
    assert "3) No worktree: edit the project folder directly" in q
    assert q.endswith("Reply 1, 2 or 3. Your request runs right after.")


def test_pending_state_is_on_disk_per_session(wenv):
    wp = _wp()
    wp.save_session_state(7, {"ask": {"prompt": "fix it", "repo": wenv.real}})
    data = json.loads(open(wenv.cfg / "worktree_pending.json").read())
    assert data["7"]["ask"]["prompt"] == "fix it"         # survives a restart: a file
    assert wp.session_state(7)["ask"]["repo"] == wenv.real
    assert wp.session_state(8) == {}
    wp.save_session_state(7, None)
    assert wp.session_state(7) == {}


def test_run_dir_and_eligible_follow_the_projects_place(wenv):
    wp = _wp()
    from aiforge_core.runtime import chat_worktree as cw
    sid = wenv.new_chat()
    wp.set_place(str(wenv.repo), "none")
    assert cw.eligible(wenv.store.get_session(sid)) is None
    assert cw.fresh_repo(wenv.store.get_session(sid)) == wenv.real
    assert cw.ensure(sid) is None
    wp.set_place(str(wenv.repo), "config")
    data = cw.ensure(sid)
    assert data["path"].startswith(cw.config_holder(wenv.real) + os.sep)
    sid2 = wenv.new_chat()
    wp.set_place(str(wenv.repo), "repo")
    assert cw.ensure(sid2)["path"].startswith(os.path.join(wenv.real, ".aiforge-worktrees") + os.sep)
