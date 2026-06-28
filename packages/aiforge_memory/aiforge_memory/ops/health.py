"""Sidecar health watchdog.

Probes Neo4j, LM Studio, embed sidecar, reranker. Writes a JSON
snapshot to ~/.aiforge/health.json on every check. Exits with non-zero
when any required sidecar is down — useful as a cron probe + alert
trigger.

Usage:
    aiforge-memory health [--once] [--json]

Cron: */1 * * * * aiforge-memory health --once >>~/.aiforge/health.cron.log 2>&1
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

HEALTH_PATH = Path(
    os.environ.get(
        "AIFORGE_HEALTH_FILE",
        os.path.expanduser("~/.aiforge/health.json"),
    )
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    info: str = ""
    latency_ms: float = 0.0
    required: bool = True


@dataclass
class HealthReport:
    ts: float = 0.0
    overall_ok: bool = True
    checks: list[CheckResult] = field(default_factory=list)


def check_all() -> HealthReport:
    out = HealthReport(ts=time.time())
    out.checks.append(_check_neo4j())
    out.checks.append(_check_lm())
    out.checks.append(_check_embed())
    out.checks.append(_check_rerank())
    out.overall_ok = all(c.ok for c in out.checks if c.required)
    return out


def _check_neo4j() -> CheckResult:
    from aiforge_memory.core.neo4j import neo4j_settings, open_driver
    uri, _, _ = neo4j_settings()
    t0 = time.perf_counter()
    try:
        drv = open_driver()
        try:
            with drv.session() as s:
                s.run("RETURN 1").consume()
            return CheckResult(
                "neo4j", True, uri,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        finally:
            drv.close()
    except Exception as exc:  # noqa: BLE001
        return CheckResult("neo4j", False, str(exc)[:160],
                           latency_ms=(time.perf_counter() - t0) * 1000)


def _check_lm() -> CheckResult:
    # Same env chain + default as translator.py / file/extract.py —
    # the probe must watch the endpoint ingest actually talks to.
    url = os.environ.get(
        "AIFORGE_CODEMEM_LM_URL",
        os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
    )
    probe = url.rstrip("/") + "/models"
    t0 = time.perf_counter()
    try:
        r = httpx.get(probe, timeout=5)
        if r.status_code == 200:
            try:
                names = [m.get("id", "?") for m in r.json().get("data", [])]
                info = f"{url} models={len(names)}"
            except Exception:  # noqa: BLE001
                info = f"{url} (no JSON)"
            return CheckResult("lm_studio", True, info,
                               latency_ms=(time.perf_counter()-t0)*1000)
        return CheckResult("lm_studio", False, f"status {r.status_code}",
                           latency_ms=(time.perf_counter()-t0)*1000)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("lm_studio", False, str(exc)[:160],
                           latency_ms=(time.perf_counter()-t0)*1000)


def _check_embed() -> CheckResult:
    url = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764")
    t0 = time.perf_counter()
    try:
        r = httpx.post(url.rstrip("/") + "/embed",
                       json={"text": "ping"}, timeout=5)
        if r.status_code == 200:
            dim = len(r.json().get("embedding") or [])
            return CheckResult("embed", True, f"{url} dim={dim}",
                               latency_ms=(time.perf_counter()-t0)*1000)
        return CheckResult("embed", False, f"status {r.status_code}",
                           latency_ms=(time.perf_counter()-t0)*1000)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("embed", False, str(exc)[:160],
                           latency_ms=(time.perf_counter()-t0)*1000)


def _check_rerank() -> CheckResult:
    url = os.environ.get("AIFORGE_RERANK_URL", "http://127.0.0.1:8765")
    t0 = time.perf_counter()
    try:
        r = httpx.post(url.rstrip("/") + "/rerank",
                       json={"query": "x", "texts": ["y"]}, timeout=5)
        if r.status_code == 200:
            return CheckResult("rerank", True, url, required=False,
                               latency_ms=(time.perf_counter()-t0)*1000)
        return CheckResult("rerank", False, f"status {r.status_code}",
                           required=False,
                           latency_ms=(time.perf_counter()-t0)*1000)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("rerank", False, str(exc)[:160],
                           required=False,
                           latency_ms=(time.perf_counter()-t0)*1000)


def write_snapshot(report: HealthReport, path: Path | None = None) -> None:
    target = path or HEALTH_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(
        {"ts": report.ts, "overall_ok": report.overall_ok,
         "checks": [asdict(c) for c in report.checks]},
        indent=2,
    ))


def render_table(report: HealthReport) -> str:
    lines = [f"# Health @ {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.ts))}"]
    lines.append("")
    lines.append("| Sidecar    | OK | Latency | Info |")
    lines.append("|------------|-----|---------|------|")
    for c in report.checks:
        ok = "✅" if c.ok else "❌"
        req = "" if c.required else " (opt)"
        lines.append(f"| {c.name}{req:6} | {ok} | {c.latency_ms:5.0f}ms | {c.info[:80]} |")
    lines.append("")
    lines.append(f"**Overall:** {'OK' if report.overall_ok else 'DEGRADED'}")
    return "\n".join(lines)
