"""Ordered learning tour over the code graph.

Deterministic order: entry points (api/main/handler-tagged or zero
in-degree on CALLS) → BFS over CALLS → de-duplicated ordered stops.
Optional ``domain`` filter restricts stops to one :Domain's symbols.
An OPTIONAL LLM pass adds a one-line note per stop; falls back to the
symbol's doc/summary. Renders a markdown tour.

Public surface:
    build_tour(driver, *, repo, domain=None, naming=None) -> TourDraft
    _order(symbols, call_edges, allow=None) -> list[str]   # pure, testable
    render_md(tour) -> str
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from aiforge_memory.features.domain.extract import (
    _bfs_chain, _entry_symbols, _label,
)

_MAX_STOPS = int(os.environ.get("AIFORGE_CODEMEM_TOUR_STOPS", "20"))


@dataclass
class TourDraft:
    repo: str
    domain: str | None = None
    name: str = "tour"
    # stops: ordered [{"node_id","kind","label","order","note"}]
    stops: list[dict] = field(default_factory=list)


# ── pure deterministic core ─────────────────────────────────────────────

def _order(symbols: list[dict], call_edges: list[tuple],
           allow: set | None = None, max_stops: int = _MAX_STOPS) -> list[str]:
    """Ordered, de-duplicated tour stops. ``allow`` (set of fqnames)
    restricts to a domain when given."""
    adj: dict[str, list[str]] = {}
    for a, b in call_edges:
        adj.setdefault(a, []).append(b)
    out: list[str] = []
    seen: set = set()
    for entry in _entry_symbols(symbols, call_edges):
        if allow is not None and entry not in allow:
            continue
        for fq in _bfs_chain(entry, adj, max_stops):
            if allow is not None and fq not in allow:
                continue
            if fq not in seen:
                seen.add(fq)
                out.append(fq)
            if len(out) >= max_stops:
                return out
    # include any allowed/leftover symbols not reached by BFS (stable order)
    for s in sorted(symbols, key=lambda x: x["fqname"]):
        fq = s["fqname"]
        if allow is not None and fq not in allow:
            continue
        if fq not in seen:
            seen.add(fq)
            out.append(fq)
        if len(out) >= max_stops:
            break
    return out


def render_md(tour: TourDraft) -> str:
    head = f"# Tour: {tour.repo}"
    if tour.domain:
        head += f" — {tour.domain}"
    lines = [head, ""]
    for st in tour.stops:
        lines.append(f"{st['order'] + 1}. **{st['label']}** (`{st['node_id']}`)")
        if st.get("note"):
            lines.append(f"   - {st['note']}")
    return "\n".join(lines) + "\n"


# ── graph reads ─────────────────────────────────────────────────────────

_Q_SYMBOLS = """
MATCH (s:Symbol_v2 {repo:$repo})
OPTIONAL MATCH (s)<-[:DEFINES]-(:File_v2)<-[:CONTAINS_FILE]-(sv:Service {repo:$repo})
RETURN s.fqname AS fqname, s.kind AS kind,
       coalesce(s.doc_first_line, s.summary, '') AS note,
       head(collect(sv.name)) AS service
"""
_Q_CALLS = """
MATCH (a:Symbol_v2 {repo:$repo})-[:CALLS]->(b:Symbol_v2 {repo:$repo})
RETURN a.fqname AS caller, b.fqname AS callee
"""
_Q_DOMAIN_SYMS = """
MATCH (d:Domain {repo:$repo, name:$domain})
OPTIONAL MATCH (d)-[:COVERS]->(:Service)-[:CONTAINS_FILE]->(:File_v2)-[:DEFINES]->(s:Symbol_v2)
OPTIONAL MATCH (d)-[:KEY_SYMBOL]->(k:Symbol_v2)
RETURN collect(DISTINCT s.fqname) + collect(DISTINCT k.fqname) AS fqnames
"""


def build_tour(driver, *, repo: str, domain: str | None = None,
               naming: bool | None = None) -> TourDraft:
    with driver.session() as sess:
        symbols = [dict(r) for r in sess.run(_Q_SYMBOLS, repo=repo)]
        call_edges = [(r["caller"], r["callee"]) for r in sess.run(_Q_CALLS, repo=repo)]
        allow = None
        if domain:
            rec = sess.run(_Q_DOMAIN_SYMS, repo=repo, domain=domain).single()
            allow = {f for f in (rec["fqnames"] if rec else []) if f}
    note_by = {s["fqname"]: (s.get("note") or "") for s in symbols}
    ordered = _order(symbols, call_edges, allow=allow)
    stops = [{"node_id": fq, "kind": "symbol", "label": _label(fq),
              "order": i, "note": note_by.get(fq, "")}
             for i, fq in enumerate(ordered)]
    tour = TourDraft(repo=repo, domain=domain,
                     name=f"tour:{domain}" if domain else "tour", stops=stops)
    if naming is None:
        naming = os.environ.get("AIFORGE_CODEMEM_NAMING", "1") not in ("0", "false")
    if naming and stops:
        try:
            _narrate(tour)
        except Exception:
            pass  # deterministic doc-line notes already set
    return tour


# ── optional LLM narration ──────────────────────────────────────────────

def _narrate(tour: TourDraft) -> None:
    import json
    from pathlib import Path
    from aiforge_memory.features.domain.extract import _call_llm, _parse
    prompt = (Path(__file__).parent / "prompts" / "tour_note.txt").read_text()
    payload = [{"id": s["order"], "label": s["label"]} for s in tour.stops]
    raw = _call_llm(system=prompt,
                    user=f"Repository: {tour.repo}\n\n{json.dumps(payload)}")
    obj = _parse(raw)
    if not obj:
        return
    for item in obj.get("notes", []):
        i = item.get("id")
        if isinstance(i, int) and 0 <= i < len(tour.stops) and item.get("note"):
            tour.stops[i]["note"] = str(item["note"])


__all__ = ["TourDraft", "build_tour", "_order", "render_md"]
