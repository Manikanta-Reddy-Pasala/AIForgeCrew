"""Group names, who owns the list, and how a client picks one.

The admin publishes; the client selects. A client naming its own group was
rejected in design: a typo silently creates a second pool that looks like a
working sync (the client pushes happily, the admin accepts happily) until
somebody asks why two machines cannot see each other's knowledge.
"""
from __future__ import annotations

import pytest

from aiforge_core.memory.sync import group


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    for k in ("AIFORGE_SYNC_GROUP", "AIFORGE_SYNC_GROUPS", "AIFORGE_ADMIN_URL"):
        monkeypatch.delenv(k, raising=False)


# ── names ────────────────────────────────────────────────────────────────

def test_a_name_that_does_not_round_trip_is_refused():
    """A group name becomes a directory component, so it takes the identity
    alphabet — refused at creation, never repaired into something else."""
    assert group.is_valid("cellular")
    assert group.is_valid("site-2_north")
    assert not group.is_valid("../etc")
    assert not group.is_valid("a b")
    assert not group.is_valid("")
    assert not group.is_valid("x" * 200)


def test_create_refuses_an_invalid_name():
    with pytest.raises(ValueError):
        group.create("../etc")


# ── the admin's list ─────────────────────────────────────────────────────

def test_no_configuration_means_ungrouped():
    """An install that predates this feature keeps working with no migration."""
    assert group.known() == []


def test_the_list_is_seeded_from_the_env(monkeypatch):
    monkeypatch.setenv("AIFORGE_SYNC_GROUPS", "cellular, retail")
    assert group.known() == ["cellular", "retail"]


def test_an_unusable_seed_name_is_dropped_not_repaired(monkeypatch):
    monkeypatch.setenv("AIFORGE_SYNC_GROUPS", "cellular, ../etc")
    assert group.known() == ["cellular"]


def test_create_persists_and_is_idempotent():
    group.create("cellular")
    group.create("cellular")
    assert group.known() == ["cellular"]


def test_an_env_seed_does_not_overwrite_a_created_list(monkeypatch):
    group.create("cellular")
    monkeypatch.setenv("AIFORGE_SYNC_GROUPS", "retail")
    assert group.known() == ["cellular"]


# ── the client's selection ───────────────────────────────────────────────

def test_env_pins_the_group_and_discovery_is_not_consulted(monkeypatch):
    monkeypatch.setenv("AIFORGE_SYNC_GROUP", "cellular")
    assert group.resolve(["retail", "other"]) == ("cellular", group.OK)


def test_exactly_one_advertised_group_is_auto_selected_and_persisted():
    assert group.resolve(["cellular"]) == ("cellular", group.OK)
    assert group.selected() == "cellular"


def test_a_chosen_group_survives_a_later_ambiguity():
    group.choose("cellular")
    assert group.resolve(["cellular", "retail"]) == ("cellular", group.OK)


def test_a_chosen_group_that_vanishes_is_kept_and_reported():
    """Clearing it would re-run auto-select and move this machine's knowledge
    into a different pool because somebody was mid-edit on the admin."""
    group.choose("cellular")
    assert group.resolve(["retail"]) == ("cellular", group.UNKNOWN)
    assert group.selected() == "cellular"


def test_an_admin_advertising_none_is_ungrouped():
    assert group.resolve([]) == ("", group.OK)


def test_a_chosen_group_against_an_ungrouped_admin_is_not_an_error():
    """The admin dropped its last group. Keep the choice, keep syncing — the
    admin's own routes decide whether the name still resolves."""
    group.choose("cellular")
    assert group.resolve([]) == ("cellular", group.OK)


def test_choose_refuses_an_invalid_name():
    with pytest.raises(ValueError):
        group.choose("../etc")


def test_an_unusable_pin_is_ignored_rather_than_used(monkeypatch):
    monkeypatch.setenv("AIFORGE_SYNC_GROUP", "../etc")
    assert group.selected() == ""
    assert group.resolve(["cellular"]) == ("cellular", group.OK)


# ── the scope ────────────────────────────────────────────────────────────

def test_scoped_repoints_the_tree_and_restores_it():
    from aiforge_core.memory.sync import _io

    before = _io.root()
    with group.scoped("cellular"):
        assert _io.root() == before / group.GROUPS_DIR / "cellular"
    assert _io.root() == before


def test_scoped_on_an_empty_name_is_a_no_op():
    """Ungrouped is a real deployment, not an error."""
    from aiforge_core.memory.sync import _io

    before = _io.root()
    with group.scoped(""):
        assert _io.root() == before


def test_scoped_restores_the_root_even_when_the_body_raises():
    from aiforge_core.memory.sync import _io

    before = _io.root()
    with pytest.raises(RuntimeError), group.scoped("cellular"):
        raise RuntimeError("boom")
    assert _io.root() == before


def test_scoped_refuses_an_invalid_name():
    with pytest.raises(ValueError), group.scoped("../etc"):
        pass


# ── the default ──────────────────────────────────────────────────────────

def test_the_default_is_the_first_group_published():
    group.create("cellular")
    group.create("retail")
    assert group.default_of(group.known()) == "cellular"


def test_several_advertised_and_none_chosen_takes_the_default():
    """Refusing to sync until somebody picks was tried first. A machine that
    quietly syncs nothing looks exactly like one that is syncing fine."""
    assert group.resolve(["cellular", "retail"]) == ("cellular", group.DEFAULTED)
    assert group.selected() == "cellular"


def test_the_default_is_persisted_so_it_is_decided_once():
    group.resolve(["cellular", "retail"])
    assert group.resolve(["cellular", "retail"]) == ("cellular", group.OK)


def test_an_explicit_choice_is_never_overridden_by_the_default():
    group.choose("retail")
    assert group.resolve(["cellular", "retail"]) == ("retail", group.OK)


def test_default_of_an_empty_list_is_ungrouped():
    assert group.default_of([]) == ""
