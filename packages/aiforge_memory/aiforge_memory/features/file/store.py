"""Cypher writer for File_v2 summary + purpose_tags."""
from __future__ import annotations

from aiforge_memory.features.file.extract import FileSummary

# Empty summary means the extract pass failed/skipped (llm_error,
# too_large, …) — keep whatever good summary the graph already holds
# instead of clobbering it. Same guard for purpose_tags.
_UPDATE_FILE = """
MATCH (f:File_v2 {repo: $repo, path: $path})
SET f.summary       = CASE WHEN $summary = ''
                           THEN coalesce(f.summary, '')
                           ELSE $summary END,
    f.purpose_tags  = CASE WHEN size($purpose_tags) = 0
                           THEN coalesce(f.purpose_tags, [])
                           ELSE $purpose_tags END,
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
