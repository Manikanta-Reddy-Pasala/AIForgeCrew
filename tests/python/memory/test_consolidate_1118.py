"""Item-level consolidation: duplicates merge without a model, OKF bullets stay."""
from aiforge_core.memory import consolidate as c


def test_exact_and_near_duplicates_merge_keeping_the_most_detailed():
    items = ["Payments settle nightly.",
             "- payments settle nightly",
             "[billing] Payments settle nightly",
             "The settlement batch runs after the regional ledger closes each day",
             "The settlement batch runs after the regional ledger closes each day, then posts",
             "Refunds go through the gateway"]
    r = c.consolidate(items)
    assert r.kept == ["Payments settle nightly.",
                      "The settlement batch runs after the regional ledger closes each day, then posts",
                      "Refunds go through the gateway"]
    assert r.removed == 3
    kept, merged = r.merges[1]
    assert "then posts" in kept and any(m.endswith("each day") for m in merged)


def test_items_with_different_numbers_never_merge():
    r = c.consolidate(["Rule 5 says batch 5 runs after region 5 closes",
                       "Rule 6 says batch 6 runs after region 6 closes",
                       "Orders sync every 5 minutes", "Orders sync every 15 minutes"])
    assert r.removed == 0


def test_different_facts_with_shared_words_stay_apart():
    r = c.consolidate(["Orders sync to the server every 5 minutes",
                       "Parties sync to the server every hour"])
    assert len(r.kept) == 2 and r.removed == 0


def test_known_items_are_not_compared_again():
    a = "The ledger closes at midnight in every region"
    b = "Refunds go through the payment gateway only"
    first = c.consolidate([a, b])
    again = c.consolidate([a, b], known={c.item_id(a), c.item_id(b)})
    assert first.kept == again.kept and again.compared == 0
    new = c.consolidate([a, b, "the ledger closes at midnight in every region!"],
                        known={c.item_id(a), c.item_id(b)})
    assert new.removed == 1


def test_report_lines_name_kept_and_merged():
    r = c.consolidate(["Alpha beta gamma delta", "alpha beta gamma delta."])
    lines = c.report_lines("fact", r)
    assert lines[0].startswith("- **fact** kept `") and lines[1].startswith("  - merged:")
