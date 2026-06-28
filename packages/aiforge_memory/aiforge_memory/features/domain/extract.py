"""Semantic domain + flow extraction over the service/symbol graph.

Deterministic-first: domains are connected components of the
service-to-service call graph; flows are ordered CALLS chains from entry
symbols. An OPTIONAL LLM pass only names/describes them — when the LM is
unreachable or ``AIFORGE_CODEMEM_NAMING=0`` we fall back to deterministic
labels, so the whole feature works offline and unit-tests need no LM.

Public surface:
    extract_domains(driver, *, repo, naming=None) -> DomainResult
    _compute(services, svc_edges, symbols, call_edges) -> (domains, flows)   # pure, testable
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Symbol kinds / name markers that make a good flow entry point.
_ENTRY_MARKERS = ("controller", "endpoint", "handler", "route", "api",
                  "main", "consumer", "listener", "scheduler")
_MAX_FLOWS = int(os.environ.get("AIFORGE_CODEMEM_MAX_FLOWS", "12"))
_MAX_FLOW_DEPTH = int(os.environ.get("AIFORGE_CODEMEM_MAX_FLOW_DEPTH", "8"))


@dataclass
class DomainDraft:
    name: str
    description: str = ""
    services: list[str] = field(default_factory=list)
    key_symbols: list[str] = field(default_factory=list)


@dataclass
class FlowDraft:
    name: str
    description: str = ""
    # steps: ordered list of {"node_id": fqname, "kind": "symbol", "label": str, "order": int}
    steps: list[dict] = field(default_factory=list)


@dataclass
class DomainResult:
    repo: str
    domains: list[DomainDraft] = field(default_factory=list)
    flows: list[FlowDraft] = field(default_factory=list)


# ── pure deterministic core (no driver, no LM) ──────────────────────────

def _connected_components(nodes: list[str], edges: list[tuple]) -> list[list[str]]:
    """Union-find over an undirected edge list. Isolated nodes form their
    own singleton component. Deterministic order (by sorted member)."""
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    for a, b in edges:
        union(a, b)
    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    comps = [sorted(members) for members in groups.values()]
    comps.sort(key=lambda m: (-len(m), m[0] if m else ""))
    return comps


def _entry_symbols(symbols: list[dict], call_edges: list[tuple]) -> list[str]:
    """Pick flow entry points: symbols with no incoming CALLS, or whose
    name/kind matches an entry marker. Deterministic sorted order."""
    callees = {b for _, b in call_edges}
    entries = []
    for s in symbols:
        fq = s["fqname"]
        low = (fq + " " + str(s.get("kind", ""))).lower()
        is_marker = any(m in low for m in _ENTRY_MARKERS)
        if fq not in callees or is_marker:
            entries.append((fq, is_marker))
    # markers first, then by name; dedupe preserving order
    entries.sort(key=lambda t: (not t[1], t[0]))
    seen, out = set(), []
    for fq, _ in entries:
        if fq not in seen:
            seen.add(fq)
            out.append(fq)
    return out


def _bfs_chain(start: str, adj: dict[str, list[str]], depth: int) -> list[str]:
    """Ordered BFS from start over the directed CALLS adjacency, capped
    at ``depth`` stops. Deterministic (sorted neighbours)."""
    order, seen, frontier = [], {start}, [start]
    while frontier and len(order) < depth:
        nxt = []
        for node in frontier:
            order.append(node)
            if len(order) >= depth:
                break
            for nb in sorted(adj.get(node, [])):
                if nb not in seen:
                    seen.add(nb)
                    nxt.append(nb)
        frontier = nxt
    return order[:depth]


def _label(fqname: str) -> str:
    """Human-ish label from an fqname — last 1-2 dotted/`::` segments."""
    parts = fqname.replace("::", ".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else fqname


def _compute(services: list[dict], svc_edges: list[tuple],
             symbols: list[dict], call_edges: list[tuple]):
    """Pure: graph data → (list[DomainDraft], list[FlowDraft]). No I/O."""
    svc_names = [s["name"] for s in services]
    sym_service = {s["fqname"]: s.get("service") for s in symbols}

    # Domains = connected components of the service call graph.
    comps = _connected_components(svc_names, svc_edges)
    domains: list[DomainDraft] = []
    for comp in comps:
        # key symbols = up to 5 symbols whose service is in this component
        keys = sorted(s["fqname"] for s in symbols
                      if s.get("service") in set(comp))[:5]
        domains.append(DomainDraft(
            name=f"domain:{comp[0]}" if comp else "domain:misc",
            description="",
            services=comp,
            key_symbols=keys,
        ))

    # Flows = ordered CALLS chains from entry symbols.
    adj: dict[str, list[str]] = {}
    for a, b in call_edges:
        adj.setdefault(a, []).append(b)
    flows: list[FlowDraft] = []
    used: set = set()
    for entry in _entry_symbols(symbols, call_edges):
        if len(flows) >= _MAX_FLOWS:
            break
        chain = _bfs_chain(entry, adj, _MAX_FLOW_DEPTH)
        if len(chain) < 2 or entry in used:
            continue
        used.update(chain)
        steps = [{"node_id": fq, "kind": "symbol", "label": _label(fq),
                  "order": i} for i, fq in enumerate(chain)]
        flows.append(FlowDraft(name=f"flow:{_label(entry)}",
                               description="", steps=steps))
    return domains, flows


# ── graph reads (driver) ────────────────────────────────────────────────

_Q_SERVICES = "MATCH (s:Service {repo:$repo}) RETURN s.name AS name, s.role AS role"

# service→service edges via symbol CALLS across CONTAINS_FILE/DEFINES
_Q_SVC_EDGES = """
MATCH (a:Service {repo:$repo})-[:CONTAINS_FILE]->(:File_v2)-[:DEFINES]->(x:Symbol_v2)
      -[:CALLS]->(y:Symbol_v2)<-[:DEFINES]-(:File_v2)<-[:CONTAINS_FILE]-(b:Service {repo:$repo})
WHERE a.name <> b.name
RETURN DISTINCT a.name AS a, b.name AS b
"""

# symbols + their owning service (first service that contains the defining file)
_Q_SYMBOLS = """
MATCH (sv:Service {repo:$repo})-[:CONTAINS_FILE]->(:File_v2)-[:DEFINES]->(s:Symbol_v2)
RETURN s.fqname AS fqname, s.kind AS kind, head(collect(sv.name)) AS service
"""

_Q_CALLS = """
MATCH (a:Symbol_v2 {repo:$repo})-[:CALLS]->(b:Symbol_v2 {repo:$repo})
RETURN a.fqname AS caller, b.fqname AS callee
"""


def _read_graph(driver, repo: str):
    with driver.session() as sess:
        services = [dict(r) for r in sess.run(_Q_SERVICES, repo=repo)]
        svc_edges = [(r["a"], r["b"]) for r in sess.run(_Q_SVC_EDGES, repo=repo)]
        symbols = [dict(r) for r in sess.run(_Q_SYMBOLS, repo=repo)]
        call_edges = [(r["caller"], r["callee"]) for r in sess.run(_Q_CALLS, repo=repo)]
    return services, svc_edges, symbols, call_edges


def extract_domains(driver, *, repo: str, naming: bool | None = None) -> DomainResult:
    """Read the graph, compute domains+flows deterministically, then
    (optionally) name them via the LM. LM failure → deterministic names."""
    services, svc_edges, symbols, call_edges = _read_graph(driver, repo)
    domains, flows = _compute(services, svc_edges, symbols, call_edges)
    if naming is None:
        naming = os.environ.get("AIFORGE_CODEMEM_NAMING", "1") not in ("0", "false")
    if naming:
        try:
            _name(domains, flows, repo=repo)
        except Exception:
            pass  # deterministic labels already in place
    return DomainResult(repo=repo, domains=domains, flows=flows)


# ── optional LLM naming (isolated for monkeypatching) ───────────────────

def _name(domains: list[DomainDraft], flows: list[FlowDraft], *, repo: str) -> None:
    """Best-effort LM naming. Mutates drafts in place. Isolated so tests
    monkeypatch ``_call_llm``."""
    import json
    from pathlib import Path
    prompt_path = Path(__file__).parent / "prompts" / "domain_name.txt"
    if not domains and not flows:
        return
    payload = {
        "domains": [{"id": i, "services": d.services,
                     "key_symbols": d.key_symbols[:5]}
                    for i, d in enumerate(domains)],
        "flows": [{"id": i, "steps": [s["label"] for s in f.steps]}
                  for i, f in enumerate(flows)],
    }
    raw = _call_llm(system=prompt_path.read_text(),
                    user=f"Repository: {repo}\n\n{json.dumps(payload)}")
    obj = _parse(raw)
    if not obj:
        return
    for d in obj.get("domains", []):
        i = d.get("id")
        if isinstance(i, int) and 0 <= i < len(domains):
            if d.get("name"):
                domains[i].name = str(d["name"])
            domains[i].description = str(d.get("description", ""))
    for f in obj.get("flows", []):
        i = f.get("id")
        if isinstance(i, int) and 0 <= i < len(flows):
            if f.get("name"):
                flows[i].name = str(f["name"])
            flows[i].description = str(f.get("description", ""))


def _parse(raw: str):
    import json
    import re
    if not raw:
        return None
    text = raw.strip().strip("`")
    if text[:4].lower() == "json":
        text = text[4:]
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _call_llm(*, system: str, user: str) -> str:
    """Real LM call. Isolated for monkeypatching in tests."""
    from openai import OpenAI

    base = os.environ.get("AIFORGE_CODEMEM_LM_URL",
                          os.environ.get("AIFORGE_INTENT_LM_URL",
                                         "http://127.0.0.1:1235/v1"))
    model = os.environ.get("AIFORGE_CODEMEM_LM_MODEL", "qwen3.6-27b-instruct")
    client = OpenAI(base_url=base,
                    api_key=os.environ.get("AIFORGE_CODEMEM_LM_KEY", "lm-studio"))
    from aiforge_memory.llm_compat import response_format
    kwargs: dict = dict(model=model, temperature=0.0, max_tokens=2000,
                        messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": user}])
    rf = response_format()
    if rf is not None:
        kwargs["response_format"] = rf
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


__all__ = ["DomainDraft", "FlowDraft", "DomainResult", "extract_domains",
           "_compute", "_connected_components", "_entry_symbols", "_bfs_chain"]
