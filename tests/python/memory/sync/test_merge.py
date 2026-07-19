"""Merge rules. Pure functions — no filesystem, no network."""
from __future__ import annotations

from aiforge_core.memory.sync import merge


def a(path: str, h: str) -> dict:
    return {"path": path, "hash": h, "cls": "A"}


def b(key: str, rev: int, by: str, h: str, *, origin: str = "nuc",
      tomb: bool = False) -> dict:
    e = {"path": f"okf/global/learnings/{key}.md", "hash": h, "cls": "B",
         "origin": origin, "key": key, "rev": rev, "updated_by": by}
    if tomb:
        e["tomb"] = True
        e["path"] = f"okf/.tomb/{origin}/{key}.json"
    return e


def test_class_a_wants_only_missing_hashes():
    local = [a("captures/x.md", "h1")]
    remote = [a("captures/x.md", "h1"), a("captures/y.md", "h2")]

    plan = merge.plan_sync(local, remote)

    assert [e["hash"] for e in plan["want"]] == ["h2"]
    assert plan["conflict"] == []


def test_class_a_same_content_different_name_is_not_wanted():
    # Content-addressed: identity is the hash, not the path.
    local = [a("captures/x.md", "h1")]
    remote = [a("captures/renamed.md", "h1")]

    assert merge.plan_sync(local, remote)["want"] == []


def test_class_b_unknown_identity_is_wanted():
    plan = merge.plan_sync([], [b("L-07", 1, "nuc", "h1")])
    assert [e["key"] for e in plan["want"]] == ["L-07"]


def test_class_b_higher_rev_wins():
    local = [b("L-07", 46, "nuc", "h1")]
    remote = [b("L-07", 47, "ms", "h2")]

    plan = merge.plan_sync(local, remote)

    assert [e["rev"] for e in plan["want"]] == [47]
    assert plan["conflict"] == []


def test_class_b_lower_rev_is_ignored():
    local = [b("L-07", 47, "ms", "h2")]
    remote = [b("L-07", 46, "nuc", "h1")]

    plan = merge.plan_sync(local, remote)

    assert plan["want"] == []
    assert plan["conflict"] == []


def test_class_b_identical_hash_is_a_no_op():
    local = [b("L-07", 47, "ms", "h2")]
    remote = [b("L-07", 47, "ms", "h2")]

    assert merge.plan_sync(local, remote) == {"want": [], "conflict": []}


def test_same_rev_different_content_is_a_conflict_with_a_deterministic_winner():
    local = [b("L-07", 47, "alice", "h1")]
    remote = [b("L-07", 47, "bob", "h2")]

    plan = merge.plan_sync(local, remote)

    # 'bob' > 'alice' lexicographically, so the remote wins and is fetched...
    assert [e["updated_by"] for e in plan["want"]] == ["bob"]
    # ...but the collision is still reported so the loser can be kept.
    assert len(plan["conflict"]) == 1
    assert plan["conflict"][0]["local"]["updated_by"] == "alice"


def test_same_rev_conflict_where_local_wins_reports_but_does_not_fetch():
    local = [b("L-07", 47, "bob", "h2")]
    remote = [b("L-07", 47, "alice", "h1")]

    plan = merge.plan_sync(local, remote)

    assert plan["want"] == []
    assert len(plan["conflict"]) == 1


def test_same_origin_and_key_across_different_scopes_is_one_identity():
    local = [b("L-07", 46, "nuc", "h1")]
    remote = [dict(b("L-07", 47, "nuc", "h2"),
                   path="okf/projects/oneshell/learnings/L-07.md")]

    plan = merge.plan_sync(local, remote)

    assert [e["rev"] for e in plan["want"]] == [47]


def test_different_origins_with_the_same_key_are_different_objects():
    local = [b("O-01", 5, "nuc", "h1", origin="nuc")]
    remote = [b("O-01", 1, "ms", "h2", origin="ms")]

    plan = merge.plan_sync(local, remote)

    assert [e["origin"] for e in plan["want"]] == ["ms"]


def test_tombstone_beats_an_older_edit():
    local = [b("L-07", 47, "nuc", "h1")]
    remote = [b("L-07", 48, "nuc", "h2", tomb=True)]

    plan = merge.plan_sync(local, remote)

    assert plan["want"][0].get("tomb") is True


def test_a_newer_edit_beats_a_tombstone():
    local = [b("L-07", 48, "nuc", "h2", tomb=True)]
    remote = [b("L-07", 49, "ms", "h3")]

    plan = merge.plan_sync(local, remote)

    assert plan["want"][0].get("tomb") is None


def test_lease_is_a_singleton_ordered_on_rev():
    local = [{"path": "okf/.lease.json", "hash": "h1", "cls": "B", "origin": "",
              "key": "__lease__", "rev": 3, "updated_by": "nuc"}]
    remote = [{"path": "okf/.lease.json", "hash": "h2", "cls": "B", "origin": "",
               "key": "__lease__", "rev": 4, "updated_by": "ms"}]

    plan = merge.plan_sync(local, remote)

    assert [e["rev"] for e in plan["want"]] == [4]
