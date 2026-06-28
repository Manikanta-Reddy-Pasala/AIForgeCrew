"""L11 — eval harness pure-python aggregation tests."""
from __future__ import annotations

from aiforge_memory.features.eval import harness as ev


def test_aggregate_perfect_recall() -> None:
    results = [
        ev.ProbeResult(
            query=f"q{i}", expected_files=[f"f{i}.py"],
            expected_symbols=[],
            returned_files=[f"f{i}.py", "x", "y"],
            returned_symbols=[], rank_first_hit=1,
            hit_at_1=True, hit_at_5=True, hit_at_10=True,
            latency_ms=100.0,
        )
        for i in range(3)
    ]
    rep = ev.aggregate("test_repo", results)
    assert rep.n == 3
    assert rep.recall_at_1 == 1.0
    assert rep.recall_at_5 == 1.0
    assert rep.mrr == 1.0


def test_aggregate_partial_recall() -> None:
    results = [
        ev.ProbeResult(
            query="q1", expected_files=["f1.py"], expected_symbols=[],
            returned_files=["f1.py"], returned_symbols=[],
            rank_first_hit=1, hit_at_1=True, hit_at_5=True, hit_at_10=True,
            latency_ms=50,
        ),
        ev.ProbeResult(
            query="q2", expected_files=["f2.py"], expected_symbols=[],
            returned_files=["x", "y", "f2.py"], returned_symbols=[],
            rank_first_hit=3, hit_at_1=False, hit_at_5=True, hit_at_10=True,
            latency_ms=200,
        ),
        ev.ProbeResult(
            query="q3", expected_files=["f3.py"], expected_symbols=[],
            returned_files=["x", "y", "z"], returned_symbols=[],
            rank_first_hit=-1, hit_at_1=False, hit_at_5=False, hit_at_10=False,
            latency_ms=150,
        ),
    ]
    rep = ev.aggregate("r", results)
    assert rep.n == 3
    assert abs(rep.recall_at_1 - 1/3) < 0.01
    assert abs(rep.recall_at_5 - 2/3) < 0.01
    # MRR = (1/1 + 1/3 + 0) / 3 = 0.4444
    assert abs(rep.mrr - (1 + 1/3) / 3) < 0.001


def test_aggregate_symbol_metrics() -> None:
    results = [
        ev.ProbeResult(
            query="q1",
            expected_files=["f.py"], expected_symbols=["a.b::run"],
            returned_files=["f.py"], returned_symbols=["a.b::run"],
            rank_first_hit=1, rank_first_sym=1,
            hit_at_1=True, hit_at_5=True, hit_at_10=True, sym_hit_at_5=True,
            latency_ms=10,
        ),
        ev.ProbeResult(
            query="q2",
            expected_files=["g.py"], expected_symbols=["x.y::go"],
            returned_files=["g.py"], returned_symbols=["a.b", "x.y::go"],
            rank_first_hit=1, rank_first_sym=2,
            hit_at_1=True, hit_at_5=True, hit_at_10=True, sym_hit_at_5=True,
            latency_ms=20,
        ),
    ]
    rep = ev.aggregate("r", results)
    assert rep.sym_recall_at_5 == 1.0
    assert abs(rep.sym_mrr - (1 + 0.5) / 2) < 0.001


def test_aggregate_handles_zero_results() -> None:
    rep = ev.aggregate("r", [])
    assert rep.n == 0
    assert rep.mrr == 0.0


def test_render_table_includes_metrics() -> None:
    rep = ev.aggregate("r", [
        ev.ProbeResult(
            query="q1", expected_files=["f.py"], expected_symbols=[],
            returned_files=["f.py"], returned_symbols=[],
            rank_first_hit=1, hit_at_1=True, hit_at_5=True, hit_at_10=True,
            latency_ms=42,
        )
    ])
    out = ev.render_table(rep)
    assert "Recall@1" in out
    assert "100.00%" in out


def test_load_probes_yaml(tmp_path) -> None:
    yaml_path = tmp_path / "p.yaml"
    yaml_path.write_text(
        "repo: foo\n"
        "probes:\n"
        "  - query: 'find bar'\n"
        "    expected_files: ['bar.py']\n"
    )
    repo, probes = ev.load_probes(yaml_path)
    assert repo == "foo"
    assert len(probes) == 1
    assert probes[0]["query"] == "find bar"
    assert probes[0]["expected_files"] == ["bar.py"]
