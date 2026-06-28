"""Eval harness — measure retrieval quality with real probes.

Probe yaml format:

    repo: PosClientBackend
    probes:
      - query: "where is JWT auth handled"
        expected_files:
          - src/main/java/com/pos/backend/feature/login/LogInValidationServiceImpl.java
        expected_symbols:
          - "...LogInValidationServiceImpl::getUserToken"
      - query: "data sync push flow"
        expected_files:
          - src/main/java/com/pos/backend/dataSync/PosServerBackendService.java

Metrics computed per probe:
    hit_at_1, hit_at_5, hit_at_10  (file-level)
    sym_hit_at_5                   (symbol-level if expected_symbols set)
    rank_first_hit                 (1-based; -1 if missed)
    rank_first_sym                 (1-based; -1 if missed)

Aggregate metrics:
    Recall@1, Recall@5, Recall@10  (mean over probes)
    MRR                             (mean reciprocal rank, file-level)
    sym_Recall@5, sym_MRR
    latency_p50_ms, latency_p95_ms, latency_mean_ms

Output:
    - JSON to stdout (always)
    - Pretty table when --table is set
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from aiforge_memory.query import bundle


@dataclass
class ProbeResult:
    query: str
    expected_files: list[str]
    expected_symbols: list[str]
    returned_files: list[str]
    returned_symbols: list[str]
    rank_first_hit: int = -1
    rank_first_sym: int = -1
    hit_at_1: bool = False
    hit_at_5: bool = False
    hit_at_10: bool = False
    sym_hit_at_5: bool = False
    latency_ms: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    repo: str
    n: int
    recall_at_1: float
    recall_at_5: float
    recall_at_10: float
    mrr: float
    sym_recall_at_5: float
    sym_mrr: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_mean_ms: float
    probes: list[ProbeResult] = field(default_factory=list)


def load_probes(path: str | Path) -> tuple[str, list[dict]]:
    raw = yaml.safe_load(Path(path).read_text())
    repo = str(raw.get("repo") or "")
    probes = list(raw.get("probes") or [])
    return repo, probes


def run_probe(
    *, query: str,
    expected_files: list[str],
    expected_symbols: list[str],
    repo: str, driver,
    token_budget: int = 4000,
) -> ProbeResult:
    pr = ProbeResult(
        query=query,
        expected_files=list(expected_files),
        expected_symbols=list(expected_symbols),
        returned_files=[],
        returned_symbols=[],
    )
    t0 = time.perf_counter()
    try:
        b = bundle.query(query, repo=repo, driver=driver, token_budget=token_budget)
    except Exception as exc:
        pr.errors.append(f"bundle: {exc}")
        pr.latency_ms = (time.perf_counter() - t0) * 1000
        return pr
    pr.latency_ms = (time.perf_counter() - t0) * 1000

    pr.returned_files = [f["path"] for f in b.files]
    pr.returned_symbols = [s["fqname"] for s in b.symbols]
    pr.errors = list(b.errors)

    expected_set = set(pr.expected_files)
    for idx, p in enumerate(pr.returned_files, 1):
        if p in expected_set:
            pr.rank_first_hit = idx
            break
    pr.hit_at_1 = pr.rank_first_hit == 1
    pr.hit_at_5 = 1 <= pr.rank_first_hit <= 5
    pr.hit_at_10 = 1 <= pr.rank_first_hit <= 10

    expected_sym_set = set(pr.expected_symbols)
    if expected_sym_set:
        for idx, sfq in enumerate(pr.returned_symbols, 1):
            if sfq in expected_sym_set or any(
                sfq.endswith(es.split("::")[-1]) for es in expected_sym_set
            ):
                pr.rank_first_sym = idx
                break
        pr.sym_hit_at_5 = 1 <= pr.rank_first_sym <= 5

    return pr


def aggregate(repo: str, results: list[ProbeResult]) -> EvalReport:
    n = len(results)
    if n == 0:
        return EvalReport(
            repo=repo, n=0, recall_at_1=0, recall_at_5=0, recall_at_10=0,
            mrr=0, sym_recall_at_5=0, sym_mrr=0,
            latency_p50_ms=0, latency_p95_ms=0, latency_mean_ms=0,
        )
    r1 = sum(1 for p in results if p.hit_at_1) / n
    r5 = sum(1 for p in results if p.hit_at_5) / n
    r10 = sum(1 for p in results if p.hit_at_10) / n
    mrr = sum(
        1 / p.rank_first_hit for p in results if p.rank_first_hit > 0
    ) / n

    sym_results = [p for p in results if p.expected_symbols]
    if sym_results:
        sym_r5 = sum(1 for p in sym_results if p.sym_hit_at_5) / len(sym_results)
        sym_mrr = sum(
            1 / p.rank_first_sym for p in sym_results if p.rank_first_sym > 0
        ) / len(sym_results)
    else:
        sym_r5 = 0.0
        sym_mrr = 0.0

    latencies = sorted([p.latency_ms for p in results])
    p50 = latencies[len(latencies) // 2]
    p95_idx = max(0, int(len(latencies) * 0.95) - 1)
    p95 = latencies[p95_idx]
    mean_lat = statistics.mean(latencies)

    return EvalReport(
        repo=repo, n=n,
        recall_at_1=r1, recall_at_5=r5, recall_at_10=r10, mrr=mrr,
        sym_recall_at_5=sym_r5, sym_mrr=sym_mrr,
        latency_p50_ms=p50, latency_p95_ms=p95, latency_mean_ms=mean_lat,
        probes=results,
    )


def run_eval(
    *, probes_path: str | Path, driver, repo: str | None = None,
    token_budget: int = 4000,
) -> EvalReport:
    yaml_repo, probes = load_probes(probes_path)
    target_repo = repo or yaml_repo
    if not target_repo:
        raise ValueError("repo not set in probes yaml or via --repo")

    results: list[ProbeResult] = []
    for p in probes:
        results.append(run_probe(
            query=str(p.get("query") or ""),
            expected_files=list(p.get("expected_files") or []),
            expected_symbols=list(p.get("expected_symbols") or []),
            repo=target_repo, driver=driver,
            token_budget=token_budget,
        ))
    return aggregate(target_repo, results)


def render_table(report: EvalReport) -> str:
    """ASCII table summary."""
    lines = [
        f"# Eval: {report.repo}  (N={report.n})",
        "",
        "| Metric          | Value |",
        "|-----------------|-------|",
        f"| Recall@1        | {report.recall_at_1:.2%} |",
        f"| Recall@5        | {report.recall_at_5:.2%} |",
        f"| Recall@10       | {report.recall_at_10:.2%} |",
        f"| MRR             | {report.mrr:.3f} |",
        f"| sym Recall@5    | {report.sym_recall_at_5:.2%} |",
        f"| sym MRR         | {report.sym_mrr:.3f} |",
        f"| latency p50 ms  | {report.latency_p50_ms:.1f} |",
        f"| latency p95 ms  | {report.latency_p95_ms:.1f} |",
        f"| latency mean ms | {report.latency_mean_ms:.1f} |",
        "",
        "## Misses",
    ]
    misses = [p for p in report.probes if not p.hit_at_5]
    if not misses:
        lines.append("(none)")
    else:
        for p in misses[:20]:
            top = p.returned_files[:3]
            lines.append(
                f"- `{p.query[:80]}` — top {top} (expected {p.expected_files[:1]})"
            )
    return "\n".join(lines)


def report_to_json(report: EvalReport) -> str:
    d = asdict(report)
    return json.dumps(d, indent=2, default=str)
