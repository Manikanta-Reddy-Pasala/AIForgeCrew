"""A small request gets a small board.

Live case: a one-bug report became 8 board items and 11 of 76 model calls
went to ticking them (OpenCode, same model: 14 calls in all).
"""
from aiforge_core.runtime.chat_agent._turn import _plan_llm as P

EIGHT = ["Find Inventory Aging report code that generates bucket labels",
         "Identify where bucket label formatting logic is implemented",
         "Fix the bucket label formatting to use singular form for 1",
         "Ensure buckets wider than one unit keep the 'X - Y' format",
         "Ensure the last open bucket retains its '> N' format",
         "Write a unit test covering all bucket label cases",
         "Run the unit test with Maven offline",
         "Run all Inventory Aging report tests to ensure no regressions"]


def test_one_bug_report_board_is_lean():
    out = P._lean(list(EIGHT), "Bug report: labels read '2 - 2 years' instead of '2 years'.")
    assert len(out) <= 4
    assert not any(t.startswith(("Find", "Identify")) for t in out)


def test_a_listed_request_keeps_its_parts(monkeypatch):
    monkeypatch.setattr(P, "sections", lambda text: ["a", "b", "c", "d", "e", "f"])
    out = P._lean([f"Do part {i}" for i in range(6)], "1. a\n2. b\n3. c\n4. d\n5. e\n6. f")
    assert len(out) == 6


def test_only_reading_tasks_are_kept_when_nothing_else():
    assert P._lean(["Find where X is defined", "Read how Y calls it"], "where is X?") == \
        ["Find where X is defined", "Read how Y calls it"]
