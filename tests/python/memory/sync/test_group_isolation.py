"""Two groups on one admin never see each other's knowledge.

This is the file that catches a leaked scope. If ``_io``'s override were an env
var rather than a ContextVar, or if one route forgot to enter the scope, one
group's node would show up in the other's manifest here — and nowhere else in
the suite would notice.
"""
from __future__ import annotations

import pytest

from tests.python.memory.sync import _hub


def _publish(admin, *names: str) -> None:
    with _hub.serving(admin):
        from aiforge_core.memory.sync import group
        for n in names:
            group.create(n)


def _manifest_keys(admin, group_name: str) -> list[str]:
    with _hub.serving(admin):
        from aiforge_core.memory.sync import group, manifest
        with group.scoped(group_name):
            return sorted(str(e.get("key")) for e in manifest.build())


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    admin = _hub.node(monkeypatch, tmp_path, "nuc")
    _publish(admin, "cellular", "retail")
    spoke = _hub.node(monkeypatch, tmp_path, "ms", admin_url="http://nuc")
    return admin, spoke


def test_several_groups_halt_a_client_that_has_not_chosen(fleet, monkeypatch):
    """Nothing is sent while the answer is ambiguous. Knowledge in the wrong
    pool is not recoverable by choosing correctly later — that pool has already
    folded it and served it onward."""
    admin, spoke = fleet
    _hub.activate(monkeypatch, spoke)
    _hub.author(spoke, "O-01", "the invoice parser rounds before tax")

    rows = _hub.run_once(monkeypatch, spoke, admin)

    assert rows[0]["state"] == "needs-group-selection"
    assert rows[0]["pushed"] == 0
    assert _manifest_keys(admin, "cellular") == []
    assert _manifest_keys(admin, "retail") == []


def test_a_chosen_group_receives_the_push_and_the_other_does_not(fleet, monkeypatch):
    admin, spoke = fleet
    _hub.activate(monkeypatch, spoke)
    from aiforge_core.memory.sync import group

    group.choose("cellular")
    _hub.author(spoke, "O-01", "the invoice parser rounds before tax")

    rows = _hub.run_once(monkeypatch, spoke, admin)

    assert rows[0]["group"] == "cellular"
    assert rows[0]["pushed"] == 1
    assert _manifest_keys(admin, "cellular") == ["O-01"]
    assert _manifest_keys(admin, "retail") == []


def test_the_ungrouped_tree_is_untouched_by_a_grouped_push(fleet, monkeypatch):
    """A grouped admin must not also be accumulating in its own top-level tree."""
    admin, spoke = fleet
    _hub.activate(monkeypatch, spoke)
    from aiforge_core.memory.sync import group

    group.choose("cellular")
    _hub.author(spoke, "O-01", "the invoice parser rounds before tax")
    _hub.run_once(monkeypatch, spoke, admin)

    assert not (admin["home"] / "md" / "peers").exists()


def test_a_single_group_admin_needs_no_client_configuration(tmp_path, monkeypatch):
    """The common deployment: one group, discovered and joined with no UI."""
    admin = _hub.node(monkeypatch, tmp_path, "nuc2")
    _publish(admin, "cellular")
    spoke = _hub.node(monkeypatch, tmp_path, "ms2", admin_url="http://nuc2")
    _hub.activate(monkeypatch, spoke)
    _hub.author(spoke, "O-01", "the invoice parser rounds before tax")

    rows = _hub.run_once(monkeypatch, spoke, admin)

    assert rows[0]["group"] == "cellular"
    assert rows[0]["pushed"] == 1
    assert _manifest_keys(admin, "cellular") == ["O-01"]


def test_an_ungrouped_admin_still_works_exactly_as_before(tmp_path, monkeypatch):
    """No groups configured anywhere: the pre-existing deployment, unmigrated."""
    admin = _hub.node(monkeypatch, tmp_path, "nuc3")
    spoke = _hub.node(monkeypatch, tmp_path, "ms3", admin_url="http://nuc3")
    _hub.activate(monkeypatch, spoke)
    _hub.author(spoke, "O-01", "the invoice parser rounds before tax")

    rows = _hub.run_once(monkeypatch, spoke, admin)

    assert rows[0]["group"] == ""
    assert rows[0]["pushed"] == 1
    assert (admin["home"] / "md" / "peers" / "ms3" / "O-01.md").is_file()


def test_two_spokes_in_different_groups_stay_apart(tmp_path, monkeypatch):
    admin = _hub.node(monkeypatch, tmp_path, "nuc4")
    _publish(admin, "cellular", "retail")
    a = _hub.node(monkeypatch, tmp_path, "msa", admin_url="http://nuc4")
    b = _hub.node(monkeypatch, tmp_path, "msb", admin_url="http://nuc4")

    _hub.activate(monkeypatch, a)
    from aiforge_core.memory.sync import group
    group.choose("cellular")
    _hub.author(a, "O-01", "the invoice parser rounds before tax")
    _hub.run_once(monkeypatch, a, admin)

    _hub.activate(monkeypatch, b)
    group.choose("retail")
    _hub.author(b, "O-02", "the stock ledger opens at the loaded period")
    _hub.run_once(monkeypatch, b, admin)

    assert _manifest_keys(admin, "cellular") == ["O-01"]
    assert _manifest_keys(admin, "retail") == ["O-02"]


def test_a_blob_is_not_readable_from_another_group(tmp_path, monkeypatch):
    admin = _hub.node(monkeypatch, tmp_path, "nuc5")
    _publish(admin, "cellular", "retail")
    spoke = _hub.node(monkeypatch, tmp_path, "ms5", admin_url="http://nuc5")
    _hub.activate(monkeypatch, spoke)
    from aiforge_core.memory.sync import group

    group.choose("cellular")
    _hub.author(spoke, "O-01", "the invoice parser rounds before tax")
    _hub.run_once(monkeypatch, spoke, admin)

    with _hub.serving(admin):
        from aiforge_core.memory.sync import manifest
        with group.scoped("cellular"):
            digest = manifest.build()[0]["hash"]
        assert admin["client"].get(f"/api/memory/sync/blob/{digest}",
                                   params={"group": "retail"}).status_code == 404
