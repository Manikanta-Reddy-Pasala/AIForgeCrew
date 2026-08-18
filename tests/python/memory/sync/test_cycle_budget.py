"""What one cycle may cost.

Per-request deadlines bound a single request; they never bounded their sum. One
manifest may advertise MAX_MANIFEST_ENTRIES (20 000) blobs and one offer may
come back asking for as many, so "check the budget when the pull is done" is not
a budget: it cannot preempt the loop that is actually spending it. Everything
here drives an admin whose own cost exceeds the whole budget — a stub that
finishes inside the budget proves only that the budget stops *starting* work.
"""
from __future__ import annotations

import time

import pytest

BLOB_DELAY = 0.25
ENTRIES = 20                 # 20 x 0.25s = 5.0s, against a 1.0s budget
BUDGET = 1.0


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    monkeypatch.setenv("AIFORGE_ADMIN_URL", "http://stub")
    monkeypatch.delenv("AIFORGE_ROLE", raising=False)


def _entries():
    return [{"kind": "A", "path": f"captures/{i:03d}.md", "hash": f"{i:064x}"}
            for i in range(ENTRIES)]


def _fat_manifest(*_a, **_k):
    return {"manifest": _entries(), "admin": "hub"}


def _slow_blob(*_a, **_k):
    time.sleep(BLOB_DELAY)
    return None              # counted as rejected; keeps the test on timing


@pytest.fixture
def sick_admin(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import loop, transport

    monkeypatch.setattr(transport, "fetch_manifest", _fat_manifest)
    monkeypatch.setattr(transport, "fetch_blob", _slow_blob)
    monkeypatch.setattr(transport, "offer", lambda *_a, **_k: [])
    monkeypatch.setattr(loop, "CYCLE_BUDGET", BUDGET)
    return loop


def test_a_slow_admin_cannot_run_the_cycle_past_the_budget(sick_admin):
    """The pull's own work is 5x the entire budget. Sampling the budget only
    after ``sync_with`` returns cannot stop it, and the fold rides the same
    loop — so an overrun costs compaction too."""
    started = time.monotonic()
    sick_admin.run_once()
    elapsed = time.monotonic() - started

    assert elapsed < BUDGET + 2.0, (
        f"cycle ran {elapsed:.1f}s on a {BUDGET}s budget — the pull alone "
        f"costs {ENTRIES * BLOB_DELAY:.1f}s")


def test_the_budget_preempts_the_pull_part_way_through_the_manifest(sick_admin):
    """Preemption has to happen *inside* the blob loop: a cycle that passes the
    pre-flight check by a millisecond must not then spend an unbounded amount
    of it. The remaining entries are still advertised next cycle."""
    rows = sick_admin.run_once()

    assert rows[0]["rejected"] < ENTRIES, (
        "the whole manifest was fetched despite the budget "
        f"({rows[0]['rejected']}/{ENTRIES} entries)")
    assert rows[0]["rejected"] > 0, "it should still have made progress"


def test_the_push_is_bounded_by_the_same_budget(monkeypatch, tmp_path):
    """Push runs first, so an unbounded push starves the pull *and* the fold
    behind it. The offer is recomputed every cycle, so the leftovers are simply
    sent next time — nothing is queued and nothing is lost."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import loop, push, transport

    # Class B nodes we minted — the only thing a spoke pushes.
    d = tmp_path / "md" / "okf" / "global" / "learnings"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(ENTRIES):
        (d / f"L-{i:03d}.md").write_text(
            f'---\ntype: learning\nid: "L-{i:03d}"\norigin: "book"\nrev: 1\n'
            f'updated_by: "book"\n---\n\nbody {i}\n', encoding="utf-8")

    sent: list = []

    def _slow_push(_base, entry, _body):
        time.sleep(BLOB_DELAY)
        sent.append(entry)
        return True

    monkeypatch.setattr(transport, "offer",
                        lambda _base, entries: list(entries))
    monkeypatch.setattr(transport, "push_blob", _slow_push)
    monkeypatch.setattr(loop, "CYCLE_BUDGET", BUDGET)

    started = time.monotonic()
    res = push.run_once("http://stub", time.monotonic() + BUDGET)
    elapsed = time.monotonic() - started

    assert 0 < len(sent) < ENTRIES, f"pushed {len(sent)}/{ENTRIES} on a budget"
    assert res["pushed"] == len(sent)
    assert elapsed < BUDGET + 2.0, f"push ran {elapsed:.1f}s"
