"""Merge rules. Pure functions — no filesystem, no network."""
from __future__ import annotations

from aiforge_core.memory.sync import merge


def a(path: str, h: str) -> dict:
    return {"path": path, "hash": h, "kind": "A"}


def b(key: str, rev: int, by: str, h: str, *, origin: str = "nuc",
      tomb: bool = False) -> dict:
    e = {"path": f"okf/global/learnings/{key}.md", "hash": h, "kind": "B",
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
    """I1: two local files carry the identity — the highest rev is the one compared."""
    local = [dict(b("L-07", 48, "nuc", "h3"),
                  path="okf/projects/oneshell/learnings/L-07.md"),
             b("L-07", 46, "nuc", "h1")]
    remote = [b("L-07", 47, "nuc", "h2")]

    plan = merge.plan_sync(local, remote)

    # rev 48 is held locally, so a remote rev 47 is stale: nothing to fetch and
    # nothing to report. Keeping the LAST local entry would want it forever.
    assert plan == {"want": [], "conflict": []}


def test_a_malformed_rev_does_not_abort_the_whole_merge():
    """B3: int('v2') used to raise, losing every well-formed entry beside it."""
    local = [b("L-01", 1, "nuc", "h0"), b("L-07", 46, "nuc", "h1")]
    remote = [dict(b("L-01", 1, "nuc", "hbad"), rev="v2"),
              b("L-07", 47, "nuc", "h2")]

    plan = merge.plan_sync(local, remote)

    # The good entry survives the bad one instead of the whole merge aborting.
    assert [e["key"] for e in plan["want"]] == ["L-07"]


def test_a_malformed_local_rev_sorts_as_zero():
    local = [dict(b("L-07", 0, "nuc", "h1"), rev="oops")]
    remote = [b("L-07", 1, "nuc", "h2")]

    assert [e["rev"] for e in merge.plan_sync(local, remote)["want"]] == [1]


def test_as_rev_coerces_anything_to_an_int():
    assert merge.as_rev(5) == 5
    assert merge.as_rev("7") == 7
    assert merge.as_rev("v2") == 0
    assert merge.as_rev(None) == 0
    assert merge.as_rev({"a": 1}) == 0


def test_equal_rev_and_equal_writer_still_resolves_to_one_winner():
    """I2: (rev, updated_by) is not total — both sides used to refuse to fetch."""
    local = [b("L-07", 47, "nuc", "h1")]
    remote = [b("L-07", 47, "nuc", "h2")]

    theirs = merge.plan_sync(local, remote)   # as seen by the local peer
    mine = merge.plan_sync(remote, local)     # as seen by the other peer

    # Exactly one of the two peers fetches; the mesh converges instead of
    # reporting the same conflict forever.
    assert len(theirs["want"]) + len(mine["want"]) == 1
    assert len(theirs["conflict"]) == 1


def test_a_remote_entry_without_a_hash_is_skipped_not_treated_as_present():
    """I3: None in the `have` set made a hash-less remote look already-held."""
    local = [a("captures/x.md", "h1")]
    remote = [{"path": "captures/y.md", "kind": "A"},
              dict(b("L-09", 1, "nuc", "h9"), hash=None)]

    plan = merge.plan_sync(local, remote)

    assert plan["want"] == []
    assert plan["conflict"] == []


def test_a_local_entry_without_a_hash_does_not_mask_a_real_remote():
    local = [{"path": "captures/x.md", "kind": "A"}]
    remote = [a("captures/y.md", "h2")]

    assert [e["hash"] for e in merge.plan_sync(local, remote)["want"]] == ["h2"]


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
