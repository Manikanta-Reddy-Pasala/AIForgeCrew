"""Graph completeness reviewer — coverage gaps in the ingested graph.

Distinct from ``features/eval`` (which measures *retrieval recall*).
This is a deterministic structural audit: which nodes/edges the ingest
left incomplete. Optional LLM pass adds a short "what's likely missing"
note from the finding set.

Public surface:
    review_graph(driver, *, repo, naming=None) -> ReviewReport
    _findings(raw) -> list[Finding]            # pure, testable
    render_md(report) -> str
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

_SAMPLE = 5  # ids per finding


@dataclass
class Finding:
    kind: str
    severity: str          # "high" | "medium" | "low"
    count: int
    sample: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class ReviewReport:
    repo: str
    findings: list[Finding] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    note: str = ""


# kind → (severity, human message)
_CHECKS = {
    "files_no_symbols":   ("medium", "files with no extracted symbols"),
    "symbols_no_edges":   ("low", "symbols with no CALLS and no inbound CALLS (orphans)"),
    "services_no_files":  ("high", "services owning no files"),
    "files_no_summary":   ("low", "files missing a summary"),
    "services_no_domain": ("low", "services not assigned to any domain"),
}


# ── pure deterministic core ─────────────────────────────────────────────

def _findings(raw: dict) -> list[Finding]:
    """raw: {kind: list[id]} → ordered Finding list (only non-empty)."""
    out: list[Finding] = []
    for kind, (sev, msg) in _CHECKS.items():
        ids = raw.get(kind) or []
        if ids:
            out.append(Finding(kind=kind, severity=sev, count=len(ids),
                               sample=list(ids)[:_SAMPLE], message=msg))
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda f: (sev_rank.get(f.severity, 3), -f.count))
    return out


def render_md(report: ReviewReport) -> str:
    lines = [f"# Graph review: {report.repo}", ""]
    t = report.totals
    lines.append(f"_nodes_: services={t.get('services', 0)} "
                 f"files={t.get('files', 0)} symbols={t.get('symbols', 0)}")
    lines.append("")
    if not report.findings:
        lines.append("No coverage gaps found. ✅")
    for f in report.findings:
        lines.append(f"- **[{f.severity}] {f.kind}** — {f.count}: {f.message}")
        if f.sample:
            lines.append(f"  - e.g. {', '.join(f.sample)}")
    if report.note:
        lines += ["", "## Note", report.note]
    return "\n".join(lines) + "\n"


# ── graph reads ─────────────────────────────────────────────────────────

_QUERIES = {
    "files_no_symbols": """
        MATCH (f:File_v2 {repo:$repo}) WHERE NOT (f)-[:DEFINES]->(:Symbol_v2)
          AND coalesce(f.test_file,false) = false
        RETURN f.path AS id ORDER BY id LIMIT 50
    """,
    "symbols_no_edges": """
        MATCH (s:Symbol_v2 {repo:$repo})
        WHERE NOT (s)-[:CALLS]->() AND NOT ()-[:CALLS]->(s)
        RETURN s.fqname AS id ORDER BY id LIMIT 50
    """,
    "services_no_files": """
        MATCH (sv:Service {repo:$repo}) WHERE NOT (sv)-[:CONTAINS_FILE]->(:File_v2)
        RETURN sv.name AS id ORDER BY id LIMIT 50
    """,
    "files_no_summary": """
        MATCH (f:File_v2 {repo:$repo})
        WHERE (f.summary IS NULL OR f.summary = '')
          AND coalesce(f.test_file,false) = false
        RETURN f.path AS id ORDER BY id LIMIT 50
    """,
    "services_no_domain": """
        MATCH (sv:Service {repo:$repo}) WHERE NOT (:Domain {repo:$repo})-[:COVERS]->(sv)
        RETURN sv.name AS id ORDER BY id LIMIT 50
    """,
}
_Q_TOTALS = """
MATCH (sv:Service {repo:$repo}) WITH count(sv) AS services
MATCH (f:File_v2 {repo:$repo}) WITH services, count(f) AS files
MATCH (s:Symbol_v2 {repo:$repo}) RETURN services, files, count(s) AS symbols
"""


def review_graph(driver, *, repo: str, naming: bool | None = None) -> ReviewReport:
    raw: dict[str, list] = {}
    totals = {"services": 0, "files": 0, "symbols": 0}
    with driver.session() as sess:
        for kind, q in _QUERIES.items():
            raw[kind] = [r["id"] for r in sess.run(q, repo=repo)]
        rec = sess.run(_Q_TOTALS, repo=repo).single()
        if rec:
            totals = {"services": rec["services"], "files": rec["files"],
                      "symbols": rec["symbols"]}
    report = ReviewReport(repo=repo, findings=_findings(raw), totals=totals)
    if naming is None:
        naming = os.environ.get("AIFORGE_CODEMEM_NAMING", "1") not in ("0", "false")
    if naming and report.findings:
        try:
            report.note = _comment(report)
        except Exception:
            pass
    return report


def _comment(report: ReviewReport) -> str:
    import json
    from pathlib import Path
    from aiforge_memory.features.domain.extract import _call_llm, _parse
    prompt = (Path(__file__).parent / "prompts" / "review_note.txt")
    sys = prompt.read_text() if prompt.exists() else (
        "Given graph coverage findings, write one short paragraph on what "
        "is likely missing from the ingest. Return JSON {\"note\":<str>}.")
    payload = [{"kind": f.kind, "count": f.count} for f in report.findings]
    raw = _call_llm(system=sys, user=json.dumps(payload))
    obj = _parse(raw)
    return str(obj.get("note", "")) if obj else ""


__all__ = ["Finding", "ReviewReport", "review_graph", "_findings", "render_md"]
