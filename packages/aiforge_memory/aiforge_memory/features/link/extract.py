"""Cross-repo edge extraction — populates `Repo-[CALLS_REPO]->Repo`.

Runs as a post-ingest pass. For each repo in scope, scans Chunk_v2.text
for evidence that the repo emits or consumes a cross-service signal:

    via=http             — Spring @RequestMapping / FastAPI route
    via=nats             — NATS publish / subscribe subject
    via=shared_collection — MongoDB collection name

Then for every (emitter_repo, consumer_repo) pair where A's emit set
intersects B's consume set, an edge is written:

    (A:Repo)-[:CALLS_REPO {
        via:        'http' | 'nats' | 'shared_collection',
        evidence:   ['/api/users','/api/auth'],
        confidence: 0.0..1.0,
        created_at: datetime
    }]->(B:Repo)

Heuristics only. Confidence reflects how many evidence tokens overlap;
operator can prune low-confidence edges in the UI.

Public surface:
    link.scan_repo(driver, repo) -> RepoEvidence
    link.compute_edges([RepoEvidence...]) -> list[CrossRepoEdge]
    link.run(driver, repos: list[str]) -> dict counts
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from aiforge_memory.features.link import store as link_writer

# ─── Regex catalog ────────────────────────────────────────────────────
# Keep patterns conservative — false positives pollute the graph.

_HTTP_SERVER = [
    re.compile(r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']'),
    re.compile(r'@(?:Get|Post|Put|Delete|Patch)Mapping\s*\(\s*["\']([^"\']+)["\']'),
    re.compile(r'@app\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']'),
    re.compile(r'@router\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']'),
]

_HTTP_CLIENT = [
    # Spring WebClient / RestTemplate
    re.compile(r'(?:restTemplate|webClient|httpClient)[^;\n]{0,200}?["\']'
               r'(https?://[^"\']+|/[A-Za-z0-9_\-/\{\}]+)["\']'),
    # Python httpx / requests
    re.compile(r'(?:httpx|requests)\.(?:get|post|put|delete|patch)\s*\(\s*'
               r'["\'](https?://[^"\']+|/[A-Za-z0-9_\-/\{\}]+)["\']'),
    # axios / fetch
    re.compile(r'(?:axios|fetch)\s*\(\s*["\']'
               r'(https?://[^"\']+|/[A-Za-z0-9_\-/\{\}]+)["\']'),
]

_NATS_PUBLISH = [
    re.compile(r'(?:publish|publishAsync|jetStream\(\)\.publish)\s*\(\s*["\']'
               r'([a-zA-Z0-9_.\-]+)["\']'),
    re.compile(r'(?:subject|SUBJECT)\s*[:=]\s*["\']([a-zA-Z0-9_.\-]+)["\']'),
]

_NATS_SUBSCRIBE = [
    re.compile(r'(?:subscribe|pullSubscribe|@Subject)\s*\(\s*["\']'
               r'([a-zA-Z0-9_.\-]+)["\']'),
    re.compile(r'@JetStreamListener\s*\([^)]*subject\s*=\s*["\']'
               r'([a-zA-Z0-9_.\-]+)'),
]

_MONGO_COLLECTION = [
    re.compile(r'@Document\s*\([^)]*collection\s*=\s*["\']([^"\']+)["\']'),
    re.compile(r'getCollection\s*\(\s*["\']([^"\']+)["\']'),
    re.compile(r'db\.([a-zA-Z][a-zA-Z0-9_]+)\.(?:find|insert|update|aggregate)'),
]


@dataclass
class RepoEvidence:
    repo: str
    http_emits: set[str] = field(default_factory=set)       # paths the repo serves
    http_consumes: set[str] = field(default_factory=set)    # URLs/paths the repo calls
    nats_emits: set[str] = field(default_factory=set)       # subjects published
    nats_consumes: set[str] = field(default_factory=set)    # subjects subscribed
    collections: set[str] = field(default_factory=set)      # mongo collections touched


@dataclass
class CrossRepoEdge:
    src: str                    # emitter repo name
    dst: str                    # consumer repo name
    via: str                    # 'http' | 'nats' | 'shared_collection'
    evidence: list[str]         # overlapping tokens (capped)
    confidence: float           # 0.0..1.0


# ─── Per-repo scan ────────────────────────────────────────────────────

_FETCH_CHUNKS = """
MATCH (c:Chunk_v2 {repo:$repo})
RETURN c.text AS text, c.file_path AS file_path
LIMIT $cap
"""


def scan_repo(driver, *, repo: str, chunk_cap: int = 5000) -> RepoEvidence:
    """Walk Chunk_v2.text for the repo, extract emit/consume sets."""
    ev = RepoEvidence(repo=repo)
    with driver.session() as s:
        rows = list(s.run(_FETCH_CHUNKS, repo=repo, cap=chunk_cap))
    for r in rows:
        text = r.get("text") or ""
        for pat in _HTTP_SERVER:
            for m in pat.finditer(text):
                _add_path(ev.http_emits, m.group(1))
        for pat in _HTTP_CLIENT:
            for m in pat.finditer(text):
                _add_path(ev.http_consumes, m.group(1))
        for pat in _NATS_PUBLISH:
            for m in pat.finditer(text):
                ev.nats_emits.add(m.group(1).strip())
        for pat in _NATS_SUBSCRIBE:
            for m in pat.finditer(text):
                ev.nats_consumes.add(m.group(1).strip())
        for pat in _MONGO_COLLECTION:
            for m in pat.finditer(text):
                ev.collections.add(m.group(1).strip())
    return ev


def _add_path(target: set[str], raw: str) -> None:
    """Normalise a captured URL/path: keep the path portion, strip
    placeholders, drop empties. ``http://host/foo/{id}`` → ``/foo/{id}``."""
    if not raw:
        return
    s = raw.strip()
    if s.startswith("http://") or s.startswith("https://"):
        # take URI path
        try:
            from urllib.parse import urlparse
            s = urlparse(s).path or s
        except Exception:
            pass
    if not s.startswith("/"):
        s = "/" + s
    if len(s) >= 2 and len(s) < 200:
        target.add(s)


# ─── Cross-repo correlation ───────────────────────────────────────────

def compute_edges(evidences: list[RepoEvidence]) -> list[CrossRepoEdge]:
    """O(N²) pairwise; N = # of repos in scope. Trivial in practice."""
    edges: list[CrossRepoEdge] = []
    for emitter in evidences:
        for consumer in evidences:
            if emitter.repo == consumer.repo:
                continue
            # HTTP: emitter.http_emits ∩ consumer.http_consumes
            http_overlap = emitter.http_emits & consumer.http_consumes
            if http_overlap:
                edges.append(CrossRepoEdge(
                    src=emitter.repo, dst=consumer.repo, via="http",
                    evidence=sorted(http_overlap)[:10],
                    confidence=_score(http_overlap, emitter.http_emits),
                ))
            # NATS: emitter.nats_emits ∩ consumer.nats_consumes
            nats_overlap = emitter.nats_emits & consumer.nats_consumes
            if nats_overlap:
                edges.append(CrossRepoEdge(
                    src=emitter.repo, dst=consumer.repo, via="nats",
                    evidence=sorted(nats_overlap)[:10],
                    confidence=_score(nats_overlap, emitter.nats_emits),
                ))
        # Shared collection — symmetric, emit only one direction (repo
        # with smaller name first) to avoid duplicates.
        for other in evidences:
            if other.repo <= emitter.repo:
                continue
            shared = emitter.collections & other.collections
            if shared:
                edges.append(CrossRepoEdge(
                    src=emitter.repo, dst=other.repo, via="shared_collection",
                    evidence=sorted(shared)[:10],
                    confidence=_score(
                        shared, emitter.collections | other.collections,
                    ),
                ))
    return edges


def _score(overlap: set[str], universe: set[str]) -> float:
    if not universe:
        return 0.0
    # Jaccard against the smaller set; clamp to [0, 1].
    return min(1.0, max(0.0, len(overlap) / max(1, len(universe))))


# ─── Orchestrator ─────────────────────────────────────────────────────

def run(
    driver, *, repos: list[str], min_confidence: float = 0.0,
) -> dict:
    """Scan each repo, compute cross-repo edges, persist them.
    Returns counts: {repos, edges, http, nats, shared_collection}."""
    evidences = [scan_repo(driver, repo=r) for r in repos]
    edges = compute_edges(evidences)
    edges = [e for e in edges if e.confidence >= min_confidence]

    counts = {
        "repos": len(repos), "edges": 0, "http": 0,
        "nats": 0, "shared_collection": 0,
    }
    for e in edges:
        link_writer.upsert_calls_repo(driver, edge=e)
        counts["edges"] += 1
        counts[e.via] = counts.get(e.via, 0) + 1
    return counts
