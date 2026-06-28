"""Cypher writer for Symbol_v2.summary.

Mirrors file_summary_writer but keyed by (repo, fqname). Empty summary
is treated as 'trivial' and we leave the property unset so consumers
can distinguish "summarised but trivial" from "not summarised yet"
via Symbol_v2.summary IS NULL.
"""
from __future__ import annotations

from aiforge_memory.features.symbol.summarise import SymbolSummary

_UPSERT = """
MATCH (s:Symbol_v2 {repo: $repo, fqname: $fqname})
SET s.summary = $summary,
    s.summary_at = datetime()
RETURN count(s) AS hit
"""


def write_symbol_summaries(
    driver, *, repo: str, summaries: list[SymbolSummary],
) -> dict:
    """Apply summaries to existing Symbol_v2 nodes.

    Returns a counter dict: {written, trivial, skipped, missing}.
    - written: persisted with non-empty summary
    - trivial: LLM returned empty (getter/delegate/etc.) — left unset
    - skipped: filter excluded the symbol upstream
    - missing: Symbol_v2 node not found (race or stale fqname)
    """
    counts = {"written": 0, "trivial": 0, "skipped": 0, "missing": 0}
    if not summaries:
        return counts
    with driver.session() as s:
        for ss in summaries:
            if ss.skipped_reason:
                if ss.skipped_reason == "trivial":
                    counts["trivial"] += 1
                else:
                    counts["skipped"] += 1
                continue
            if not ss.summary:
                counts["trivial"] += 1
                continue
            r = s.run(_UPSERT, repo=repo, fqname=ss.fqname,
                      summary=ss.summary).single()
            if r and r["hit"] > 0:
                counts["written"] += 1
            else:
                counts["missing"] += 1
    return counts
