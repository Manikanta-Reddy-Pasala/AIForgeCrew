"""N9 — token-budget trim must drop code chunks before memory facts
(decisions / observations / notes / docs)."""
from __future__ import annotations

from aiforge_memory.query.bundle import ContextBundle, _trim_to_budget


def _fat_bundle() -> ContextBundle:
    return ContextBundle(
        repo="r",
        chunks=[{"file_path": f"f{i}.py", "text": "code " * 200}
                for i in range(5)],
        decisions=[{"title": "use NATS", "rationale": "r" * 50,
                    "status": "active"}],
        observations=[{"kind": "gotcha", "text": "o" * 100}],
        notes=[{"title": "note", "body": "n" * 100}],
    )


def test_chunks_dropped_before_memory_facts():
    b = _fat_bundle()
    # Budget big enough for facts but far too small for 5 fat chunks.
    _trim_to_budget(b, token_budget=300)
    assert b.chunks == []
    assert b.decisions, "decisions must survive chunk trimming"
    assert b.observations, "observations must survive chunk trimming"
    assert b.notes, "notes must survive chunk trimming"


def test_memory_facts_still_dropped_when_budget_tiny():
    b = _fat_bundle()
    b.symbols = [{"fqname": f"a::f{i}", "signature": "sig"} for i in range(8)]
    _trim_to_budget(b, token_budget=10)
    assert b.chunks == []
    assert b.decisions == []
    assert b.observations == []


def test_within_budget_is_untouched():
    b = _fat_bundle()
    _trim_to_budget(b, token_budget=10_000)
    assert len(b.chunks) == 5
    assert b.decisions and b.observations and b.notes
