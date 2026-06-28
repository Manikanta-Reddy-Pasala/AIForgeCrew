"""Optional Cypher writer for :ReviewFinding nodes (trend tracking).

Public surface:
    upsert_review(driver, *, repo, report, run_id) -> dict counts

Findings are keyed (repo, run_id, kind) so multiple audits accumulate
for trend queries; callers that don't want history simply skip this.
"""
from __future__ import annotations

_UPSERT_FINDING = """
MERGE (rf:ReviewFinding {repo:$repo, run_id:$run_id, kind:$kind})
SET rf.severity = $severity, rf.count = $count, rf.sample = $sample,
    rf.message = $message, rf.schema_version = 'codemem-v1'
WITH rf
MATCH (r:Repo {name:$repo})
MERGE (r)-[:HAS_REVIEW]->(rf)
"""


def upsert_review(driver, *, repo, report, run_id) -> dict:
    n = 0
    with driver.session() as sess:
        for f in report.findings:
            sess.run(_UPSERT_FINDING, repo=repo, run_id=run_id, kind=f.kind,
                     severity=f.severity, count=f.count, sample=f.sample,
                     message=f.message).consume()
            n += 1
    return {"findings": n}


__all__ = ["upsert_review"]
