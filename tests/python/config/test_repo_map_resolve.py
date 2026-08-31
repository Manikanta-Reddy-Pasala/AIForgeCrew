"""Finding a repo's folder from a name someone typed in chat.

Nobody types "PosClientBackend" the same way twice — "pos client backend",
"posclient", "PosClinetBackend". So the matcher normalises (lowercase, drop
every non-alphanumeric) and then tries three stages in order: exact on the
normalised token, difflib fuzzy, and finally substring. Each stage refuses to
guess when two DIFFERENT folders tie, because silently picking one would send
a ticket's work into the wrong repo; aliases pointing at the SAME folder are
not a tie and resolve fine.

The same matcher backs Jira projects and Confluence spaces, which is why it
takes a candidate map and a value key rather than knowing about folders.
"""
from __future__ import annotations

import json

import pytest

from aiforge_core.config import repo_map as M


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    """An isolated config dir, so no test reads the operator's real repos."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_WORKTREE_ROOT", raising=False)
    return tmp_path


def _repos(tmp_path, *names):
    root = tmp_path / "codeRepo"
    root.mkdir(exist_ok=True)
    for n in names:
        (root / n).mkdir()
    M.set_default_root(str(root))
    return root


# ─── the base folder ───────────────────────────────────────────────────


def test_the_stored_base_folder_wins(cfg):
    assert M.set_default_root("~/work/repos")["ok"] is True
    assert M.default_root().endswith("/work/repos")


def test_the_env_is_read_live_when_nothing_is_stored(cfg, monkeypatch):
    monkeypatch.setenv("AIFORGE_WORKTREE_ROOT", "/srv/repos")
    assert M.default_root() == "/srv/repos"


def test_the_last_resort_is_the_conventional_folder(cfg):
    assert M.default_root().endswith("/codeRepo")


def test_a_blank_base_folder_is_refused(cfg):
    assert M.set_default_root("  ") == {"ok": False, "error": "path required"}


# ─── explicit per-repo mappings ────────────────────────────────────────


def test_a_repo_outside_the_base_folder_can_be_pinned(cfg):
    assert M.set_path("widgets", "/srv/elsewhere/widgets")["ok"] is True
    assert M.get_path("widgets") == "/srv/elsewhere/widgets"


def test_a_pinned_repo_is_found_however_it_is_cased(cfg):
    M.set_path("PosClientBackend", "/srv/pcb")
    assert M.get_path("posclientbackend") == "/srv/pcb"


def test_a_home_relative_path_is_expanded(cfg):
    M.set_path("w", "~/w")
    assert not M.get_path("w").startswith("~")


def test_an_unknown_repo_has_no_pinned_path(cfg):
    assert M.get_path("ghost") is None
    assert M.get_path("") is None


def test_both_a_name_and_a_path_are_required(cfg):
    assert M.set_path("", "/x")["ok"] is False
    assert M.set_path("x", " ")["ok"] is False


def test_a_mapping_can_be_removed(cfg):
    M.set_path("w", "/srv/w")
    assert M.delete_path("w") == {"ok": True, "removed": "w"}
    assert M.get_path("w") is None


def test_removing_something_that_was_never_mapped_says_so(cfg):
    assert M.delete_path("ghost")["ok"] is False


def test_everything_known_is_listed(cfg):
    M.set_default_root("/srv/repos")
    M.set_path("w", "/elsewhere/w")
    assert M.list_all() == {"default_root": "/srv/repos",
                            "paths": {"w": "/elsewhere/w"}}


# ─── the stored file ───────────────────────────────────────────────────


def test_the_mapping_survives_a_restart(cfg):
    M.set_path("w", "/srv/w")
    assert json.loads((cfg / "repos.json").read_text())["paths"] == {"w": "/srv/w"}


def test_a_corrupt_config_file_reads_as_empty(cfg):
    (cfg / "repos.json").write_text("{not json")
    assert M.list_all()["paths"] == {}


def test_a_config_file_holding_the_wrong_shape_is_ignored(cfg):
    (cfg / "repos.json").write_text("[1, 2, 3]")
    assert M.list_all()["paths"] == {}


# ─── the loose-name matcher ────────────────────────────────────────────


@pytest.mark.parametrize("typed", ["PosClientBackend", "pos client backend",
                                   "pos_client-backend", "POSCLIENTBACKEND"])
def test_a_name_typed_any_way_finds_the_same_repo(typed):
    res = M.fuzzy_pick(typed, {"PosClientBackend": "/srv/pcb"})
    assert res["ok"] is True and res["value"] == "/srv/pcb"
    assert res["match"] == "normalized"


def test_a_small_typo_still_resolves():
    res = M.fuzzy_pick("PosClinetBackend", {"PosClientBackend": "/srv/pcb"})
    assert res["ok"] is True and res["match"] == "fuzzy"


def test_a_fragment_of_the_name_falls_through_to_substring():
    res = M.fuzzy_pick("client", {"PosClientBackend": "/srv/pcb"})
    assert res["ok"] is True and res["match"] == "substring"


def test_two_different_repos_tying_is_never_guessed():
    """Picking one would send the work into the wrong repo."""
    res = M.fuzzy_pick("backend", {"backend": "/a", "Back End": "/b"})
    assert res["ok"] is False and res["error"] == "ambiguous"
    assert sorted(res["candidates"]) == ["Back End", "backend"]


def test_two_names_for_the_same_folder_are_not_a_tie():
    res = M.fuzzy_pick("backend", {"backend": "/a", "Back-End": "/a"})
    assert res["ok"] is True and res["value"] == "/a"


def test_a_close_fuzzy_race_is_ambiguous():
    res = M.fuzzy_pick("servicex", {"servicea": "/a", "serviceb": "/b"})
    assert res["ok"] is False and res["error"] == "ambiguous"


def test_a_clear_fuzzy_winner_is_taken():
    res = M.fuzzy_pick("posclientbackend",
                       {"PosClientBackend": "/a", "PosServerBackend": "/b"})
    assert res["ok"] is True and res["value"] == "/a"


def test_two_substring_hits_are_ambiguous():
    res = M.fuzzy_pick("service", {"service-one": "/a", "service-two": "/b"})
    assert res["ok"] is False and res["error"] == "ambiguous"


def test_a_name_matching_nothing_offers_what_there_is():
    res = M.fuzzy_pick("zzz", {"alpha": "/a", "beta": "/b"})
    assert res["error"] == "no match" and res["candidates"] == ["alpha", "beta"]


def test_the_candidate_list_offered_back_is_capped():
    cands = {f"repo{i:02d}": f"/r{i}" for i in range(30)}
    assert len(M.fuzzy_pick("zzz", cands)["candidates"]) == 10


def test_nothing_to_match_against_is_its_own_answer():
    assert M.fuzzy_pick("x", {})["error"] == "no candidates"
    assert M.fuzzy_pick("  ", {"a": "/a"})["error"] == "name required"


def test_the_matcher_serves_other_registries_too():
    """The same code backs Jira projects and Confluence spaces."""
    res = M.fuzzy_pick("one shell", {"OneShell": "ONE"}, value_key="key")
    assert res["key"] == "ONE" and res["name"] == "OneShell"


@pytest.mark.parametrize("a,b", [("Pos Client-Backend", "posclientbackend"),
                                 ("pos_client backend", "posclientbackend")])
def test_the_normalised_token_collapses_punctuation_and_case(a, b):
    assert M._norm(a) == M._norm(b)


# ─── resolving a repo end to end ───────────────────────────────────────


def test_a_pinned_folder_that_exists_wins_outright(cfg, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    M.set_path("widgets", str(real))
    assert M.resolve("widgets") == {"ok": True, "path": str(real),
                                    "name": "widgets", "match": "explicit"}


def test_a_pinned_folder_that_is_gone_still_shadows_the_base_folder(cfg,
                                                                    tmp_path):
    """The explicit mapping stays a candidate even when it no longer exists,
    so the answer names the stale path rather than the same-named folder under
    the base — worth knowing when a repo has been moved."""
    _repos(tmp_path, "widgets")
    M.set_path("widgets", "/gone/widgets")
    res = M.resolve("widgets")
    assert res["ok"] is True and res["path"] == "/gone/widgets"


def test_a_moved_repo_is_found_under_the_base_folder(cfg, tmp_path):
    _repos(tmp_path, "widgets")
    M.set_path("widgets-old", "/gone/widgets")
    res = M.resolve("widgets")
    assert res["ok"] is True and res["path"].endswith("codeRepo/widgets")


def test_the_folders_under_the_base_are_candidates(cfg, tmp_path):
    _repos(tmp_path, "AIForgeCrew", "PosFrontend")
    res = M.resolve("pos frontend")
    assert res["ok"] is True and res["path"].endswith("PosFrontend")


def test_hidden_folders_are_not_repos(cfg, tmp_path):
    root = _repos(tmp_path, "widgets")
    (root / ".cache").mkdir()
    assert M.resolve("cache")["ok"] is False


def test_a_file_in_the_base_folder_is_not_a_repo(cfg, tmp_path):
    root = _repos(tmp_path, "widgets")
    (root / "notes.md").write_text("x")
    assert M.resolve("notes")["ok"] is False


def test_nothing_configured_at_all_says_how_to_fix_it(cfg, monkeypatch):
    monkeypatch.setenv("AIFORGE_WORKTREE_ROOT", "/definitely/not/here")
    res = M.resolve("widgets")
    assert res["ok"] is False and "set_repo_root" in res["error"]


def test_an_unreadable_base_folder_is_not_fatal(cfg, monkeypatch, tmp_path):
    M.set_default_root(str(tmp_path / "root"))
    (tmp_path / "root").mkdir()
    monkeypatch.setattr(M.os, "scandir",
                        lambda p: (_ for _ in ()).throw(OSError("perm")))
    M.set_path("widgets", "/srv/widgets")
    assert M.resolve("widgets")["ok"] is True, "the explicit mapping still works"


def test_a_pinned_repo_outside_the_base_is_still_a_candidate(cfg, tmp_path):
    _repos(tmp_path, "other")
    M.set_path("Widgets", "/srv/widgets")
    res = M.resolve("widgets")
    assert res["ok"] is True and res["path"] == "/srv/widgets"
