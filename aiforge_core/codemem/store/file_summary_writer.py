"""Cypher writer for File_v2 summary + purpose_tags."""
from __future__ import annotations

from aiforge_core.codemem.ingest.file_summary import FileSummary


_UPDATE_FILE = """
MATCH (f:File_v2 {repo: $repo, path: $path})
SET f.summary       = $summary,
    f.purpose_tags  = $purpose_tags,
    f.skipped_reason = $skipped_reason
"""


def write_summaries(driver, *, repo: str, summaries: list[FileSummary]) -> dict:
    counts = {"updated": 0, "skipped": 0}
    with driver.session() as sess:
        for fs in summaries:
            if fs.skipped_reason and not fs.summary:
                counts["skipped"] += 1
            else:
                counts["updated"] += 1
            sess.run(
                _UPDATE_FILE,
                repo=repo, path=fs.path,
                summary=fs.summary,
                purpose_tags=fs.purpose_tags,
                skipped_reason=fs.skipped_reason,
            ).consume()
    return counts
