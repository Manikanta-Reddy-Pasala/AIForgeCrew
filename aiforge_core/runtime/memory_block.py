"""Pre-flight AiForgeMemory recall used at ticket-claim time.

Pulls the same hybrid hits the Doer's :func:`memory_lookup` tool would
return (vector + fulltext + 1-hop graph, fused via RRF), formats them
as a markdown block, and hands the string to ``adk_runner`` to append
to the pipeline's first prompt. Empty string on any failure — never
raises, never blocks the ticket.
"""
from __future__ import annotations

import logging

log = logging.getLogger("aiforge.memory_block")


def _recall_hits(ticket) -> "tuple[dict | None, str]":
    """Run unified recall for a ticket → (result, query_text). result is None
    when the backend is unreachable / errored."""
    try:
        from aiforge_core.memory import unified_query as _uq
        text = f"{ticket.title}\n{ticket.body or ''}"
        # ticket.project doubles as the AiForgeMemory Repo.name for source #7
        # (afm_bundle). Falls back to env when absent.
        result = _uq.query(text, ticket=ticket.identifier, limit=8,
                           repo=getattr(ticket, "project", None) or None)
        return result, text
    except Exception as exc:
        log.warning("memory recall failed: %s", exc)
        return None, ""


def _project_brief_prefix(ticket) -> str:
    """The consolidated OKR brief (project ∪ linked ∪ global) — the same curated
    memory chat injects. Without this the Doer saw only whatever raw hits ranked;
    the deduped/superseded/mapped brief never reached the pipeline (audit R2)."""
    try:
        from aiforge_core.runtime.context_bundle import project_brief_text
        pb = project_brief_text(getattr(ticket, "project", "") or "")
        return f"## Project memory (OKR briefs)\n\n{pb}\n\n" if pb else ""
    except Exception:  # noqa: BLE001
        return ""


def _render_hit_lines(prefix: str, hits: list, result: dict) -> str:
    """Raw hit list fallback when the map→summarize briefing is empty."""
    lines = [prefix + "## Memory hits (AiForgeMemory)", ""]
    for h in hits[:8]:
        try:
            score = float(h.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        body = (h.get("text") or h.get("body") or h.get("summary") or "")[:300]
        lines.append(f"- [{h.get('source', '?')} {score:.2f}] {body}")
    log.info("memory: %d hits sources=%s", len(hits),
             ",".join(result.get("used_sources") or []))
    return "\n".join(lines) + "\n"


def fetch(ticket) -> str:
    """Return a markdown ``## Memory hits`` block, or ``""`` when the backend is
    unreachable / produced nothing."""
    result, text = _recall_hits(ticket)
    if result is None:
        return ""
    prefix = _project_brief_prefix(ticket)
    hits = result.get("hits") or []
    if not hits:
        return prefix
    # Map→summarize: when many scattered hits come back, fold them into ONE
    # compact briefing (LLM) instead of dumping snippets. Empty → keep raw list.
    try:
        from aiforge_core.memory import recall_summary
        brief = recall_summary.summarize_hits(text, hits)
    except Exception:  # noqa: BLE001
        brief = ""
    if brief:
        sources = ",".join(result.get("used_sources") or [])
        log.info("memory: %d hits summarized (sources=%s)", len(hits), sources)
        return prefix + f"## Memory briefing (AiForgeMemory)\n\n{brief}\n"
    return _render_hit_lines(prefix, hits, result)


__all__ = ["fetch"]
