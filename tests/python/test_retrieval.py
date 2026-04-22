from unittest.mock import patch
from aiforge_core.retrieval import rrf_fuse, Hit, retrieve_for_role, ROLE_POLICIES


def test_rrf_fuse_reinforces_agreed():
    bm25 = [Hit(id="a", score=0.0), Hit(id="b", score=0.0), Hit(id="c", score=0.0)]
    vec  = [Hit(id="b", score=0.0), Hit(id="a", score=0.0), Hit(id="d", score=0.0)]
    merged = rrf_fuse([bm25, vec], k=60, top_n=4)
    ids = [h.id for h in merged]
    # a and b appear in both lists → should rank highest
    assert ids.index("a") < ids.index("c")
    assert ids.index("b") < ids.index("d")


def test_role_policies_defined_for_expected_roles():
    expected = {
        "architect", "sr_developer", "developer", "fact_extract",
        "supervisor", "planner", "doer", "feedback", "learner",
    }
    assert expected <= set(ROLE_POLICIES)
    for pol in ROLE_POLICIES.values():
        assert "tiers" in pol
        assert "rerank_keep" in pol


def test_retrieve_for_role_calls_tiers_in_policy_order():
    calls = []

    class FakeStore:
        def search_tier_bm25(self, tier, query, top_k, wing_prefix=None):
            calls.append(("bm25", tier, top_k))
            return []
        def search_tier_vec(self, tier, query, top_k, wing_prefix=None):
            calls.append(("vec", tier, top_k))
            return []

    with patch("aiforge_core.retrieval.rerank_http", side_effect=lambda q, h, keep: h[:keep]):
        retrieve_for_role(FakeStore(), role="developer", query="x", parent_id=None)

    tiers_queried = [c[1] for c in calls if c[0] == "bm25"]
    policy = ROLE_POLICIES["developer"]["tiers"]
    assert tiers_queried == [t["tier"] for t in policy]
