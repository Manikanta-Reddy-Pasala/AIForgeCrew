"""The $/turn tracker: what a call costs, and what the totals then say.

Nothing exercised this module. Every rate lookup, every tally and the
kill-switch were carried by whatever happened to import them — which is how a
"cost data is observability, not load-bearing" module ends up quietly
returning zero for every model.
"""
from __future__ import annotations

import pytest

from aiforge_core.observability import cost


@pytest.fixture(autouse=True)
def _clean_totals(monkeypatch):
    """Each test starts with empty books and tracking ON."""
    monkeypatch.setattr(cost, "_TOTALS", {})
    monkeypatch.setattr(cost, "_GLOBAL",
                        {"usd": 0.0, "prompt": 0, "completion": 0, "calls": 0})
    monkeypatch.delenv("AIFORGE_COST_TRACKING", raising=False)


# ── the rate table ──────────────────────────────────────────────────────────

def test_a_known_model_is_priced_per_million_tokens():
    # gpt-oss:120b is $0.50/Mtok in, $1.50/Mtok out.
    usd = cost.usd_for("gpt-oss:120b",
                       prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert usd == pytest.approx(2.0)


def test_an_unknown_model_costs_nothing_rather_than_raising():
    """The gauge sits on the run path; a model we have no price for must not
    take the run down with it."""
    assert cost.usd_for("some-model-nobody-priced",
                        prompt_tokens=10_000, completion_tokens=10_000) == 0.0


@pytest.mark.parametrize("given,short", [
    ("openai/gpt-oss:20b", "gpt-oss:20b"),
    ("ollama/gpt-oss:20b", "gpt-oss:20b"),
    ("ollama_cloud/glm-4.7", "glm-4.7"),
    ("/models/mlx/Qwen3-Coder-Next-MLX-4bit", "Qwen3-Coder-Next-MLX-4bit"),
    ("gpt-oss:20b", "gpt-oss:20b"),
])
def test_provider_prefixes_and_paths_resolve_to_the_priced_name(given, short):
    assert cost._short_model_name(given) == short
    assert cost.usd_for(given, prompt_tokens=1_000_000,
                        completion_tokens=0) == cost.usd_for(
        short, prompt_tokens=1_000_000, completion_tokens=0)


def test_an_operator_can_reprice_a_model_at_runtime(monkeypatch):
    monkeypatch.setitem(cost.RATES, "gpt-oss:20b", (0.10, 0.30))
    cost.reset_table("gpt-oss:20b", 1.0, 2.0)
    assert cost.usd_for("gpt-oss:20b",
                        prompt_tokens=1_000_000,
                        completion_tokens=1_000_000) == pytest.approx(3.0)


# ── tallying ────────────────────────────────────────────────────────────────

def test_a_call_lands_in_both_the_ticket_and_the_global_total():
    out = cost.record_call(role="doer", ticket="ONE-1", model="gpt-oss:120b",
                           prompt_tokens=1_000_000, completion_tokens=0)
    assert out["usd"] == pytest.approx(0.5)
    assert out["ticket_total"] == pytest.approx(0.5)

    snap = cost.snapshot("ONE-1")
    assert snap["calls"] == 1
    assert snap["prompt"] == 1_000_000
    assert cost.snapshot()["global"]["usd"] == pytest.approx(0.5)


def test_a_second_call_accumulates_on_the_same_ticket():
    for _ in range(3):
        out = cost.record_call(role="doer", ticket="ONE-2",
                               model="gpt-oss:120b",
                               prompt_tokens=1_000_000, completion_tokens=0)
    assert out["ticket_total"] == pytest.approx(1.5)
    assert cost.snapshot("ONE-2")["calls"] == 3


def test_a_call_with_no_ticket_still_moves_the_global_total():
    out = cost.record_call(role="chat", ticket=None, model="gpt-oss:120b",
                           prompt_tokens=1_000_000, completion_tokens=0)
    assert out["ticket_total"] == 0.0
    assert cost.snapshot()["global"]["calls"] == 1
    assert cost.snapshot()["tickets"] == {}


def test_snapshot_of_an_unknown_ticket_is_zeros_not_an_error():
    assert cost.snapshot("never-seen") == {
        "usd": 0.0, "prompt": 0, "completion": 0, "calls": 0}


def test_snapshot_hands_back_a_copy(monkeypatch):
    """A caller mutating the snapshot must not edit the books."""
    cost.record_call(role="doer", ticket="ONE-3", model="gpt-oss:120b",
                     prompt_tokens=1_000_000, completion_tokens=0)
    snap = cost.snapshot("ONE-3")
    snap["usd"] = 999.0
    assert cost.snapshot("ONE-3")["usd"] == pytest.approx(0.5)


def test_the_kill_switch_stops_the_tally_entirely(monkeypatch):
    monkeypatch.setenv("AIFORGE_COST_TRACKING", "0")
    out = cost.record_call(role="doer", ticket="ONE-4", model="gpt-oss:120b",
                           prompt_tokens=1_000_000, completion_tokens=0)
    assert out == {"usd": 0.0, "ticket_total": 0.0}
    assert cost.snapshot()["global"]["calls"] == 0


def test_a_failing_persist_does_not_lose_the_in_memory_tally(monkeypatch):
    """`record_call` calls the persist hook best-effort; the totals it just
    computed must survive the hook blowing up."""
    def _boom(**_kw):
        raise RuntimeError("no database here")

    monkeypatch.setattr(cost, "_persist", _boom)
    out = cost.record_call(role="doer", ticket="ONE-5", model="gpt-oss:120b",
                           prompt_tokens=1_000_000, completion_tokens=0)
    assert out["usd"] == pytest.approx(0.5)
    assert cost.snapshot("ONE-5")["calls"] == 1


# ── the degraded rollup ─────────────────────────────────────────────────────

@pytest.mark.parametrize("group_by", ["day", "role", "model", "ticket"])
def test_rollup_is_empty_but_accepts_every_documented_grouping(group_by):
    assert cost.rollup(group_by) == []


def test_rollup_still_rejects_a_grouping_it_never_supported():
    """The store is gone; the contract is not. A caller asking for a grouping
    that never existed should hear about it rather than get [] back."""
    with pytest.raises(ValueError, match="day|role|model|ticket"):
        cost.rollup("by_phase_of_moon")


def test_persist_is_a_no_op_that_does_not_raise():
    assert cost._persist(role="doer", ticket="ONE-6", model="m",
                         cost_usd=1.0, prompt_tokens=1,
                         completion_tokens=1) is None
