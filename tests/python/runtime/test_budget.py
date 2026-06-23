from __future__ import annotations

import json
import threading

from aiforge_core.runtime.budget import BudgetTracker, Spend


def test_record_returns_spend_with_ts():
    t = BudgetTracker()
    s = t.record("doer", "qwen-coder-next", input_tokens=100,
                 output_tokens=50, cost_usd=0.001)
    assert isinstance(s, Spend)
    assert s.role == "doer"
    assert s.input_tokens == 100
    assert s.output_tokens == 50
    assert s.cost_usd == 0.001
    assert s.ts > 0


def test_total_sums_records():
    t = BudgetTracker()
    t.record("doer", "m", input_tokens=10, output_tokens=5, cost_usd=0.001)
    t.record("planner", "m", input_tokens=20, output_tokens=10, cost_usd=0.002)
    tot = t.total()
    assert tot["input_tokens"] == 30
    assert tot["output_tokens"] == 15
    assert abs(tot["cost_usd"] - 0.003) < 1e-9
    assert tot["calls"] == 2


def test_by_role_aggregation():
    t = BudgetTracker()
    t.record("doer", "m", input_tokens=10, output_tokens=5, cost_usd=0.001)
    t.record("doer", "m", input_tokens=20, output_tokens=10, cost_usd=0.002)
    t.record("planner", "n", input_tokens=5, output_tokens=2, cost_usd=0.0005)
    by_role = t.by_role()
    assert by_role["doer"]["calls"] == 2
    assert by_role["doer"]["input_tokens"] == 30
    assert by_role["planner"]["calls"] == 1


def test_by_model_aggregation():
    t = BudgetTracker()
    t.record("doer", "qwen", input_tokens=10, output_tokens=5)
    t.record("planner", "qwen", input_tokens=20, output_tokens=10)
    t.record("doer", "glm", input_tokens=5, output_tokens=2)
    by_model = t.by_model()
    assert by_model["qwen"]["calls"] == 2
    assert by_model["glm"]["calls"] == 1


def test_ring_evicts_at_cap():
    t = BudgetTracker(cap=3)
    for i in range(5):
        t.record(f"role{i}", "m", input_tokens=1)
    assert t.total()["calls"] == 3
    roles = list(t.by_role().keys())
    # Oldest two (role0, role1) evicted
    assert "role0" not in roles
    assert "role4" in roles


def test_reset_clears():
    t = BudgetTracker()
    t.record("doer", "m", input_tokens=1)
    assert t.total()["calls"] == 1
    t.reset()
    assert t.total()["calls"] == 0


def test_to_json_round_trip():
    t = BudgetTracker()
    t.record("doer", "m", input_tokens=10, output_tokens=5, cost_usd=0.001)
    data = json.loads(t.to_json())
    assert len(data) == 1
    assert data[0]["role"] == "doer"
    assert data[0]["input_tokens"] == 10


def test_thread_safety_records_lost():
    t = BudgetTracker(cap=10_000)
    def _push():
        for _ in range(200):
            t.record("doer", "m", input_tokens=1)
    threads = [threading.Thread(target=_push) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert t.total()["calls"] == 1000


def test_module_singleton_exposed():
    from aiforge_core.runtime.budget import tracker
    assert isinstance(tracker, BudgetTracker)
