"""L9 — pure-python tests for cross-repo evidence extraction + edge fusion."""
from __future__ import annotations

from aiforge_memory.features.link import extract as link

# ─── HTTP server route capture ────────────────────────────────────────

def test_request_mapping_extracted(driver_stub: type | None = None) -> None:
    text = '''
    @RestController
    @RequestMapping("/api/users")
    public class UserController {
        @GetMapping("/{id}")
        public User get(@PathVariable String id) { return svc.find(id); }
    }
    '''
    ev = _extract_from_text(text)
    assert "/api/users" in ev.http_emits
    assert "/{id}" in ev.http_emits


def test_fastapi_route_extracted() -> None:
    text = '''
    @app.get("/health")
    def health(): return {"ok": True}

    @router.post("/items")
    def add(item: Item): ...
    '''
    ev = _extract_from_text(text)
    assert "/health" in ev.http_emits
    assert "/items" in ev.http_emits


def test_http_client_extracted() -> None:
    text = '''
    restTemplate.postForObject("https://other.svc/api/users", body, X.class);
    httpx.get("https://other.svc/health")
    fetch("/internal/echo")
    '''
    ev = _extract_from_text(text)
    assert "/api/users" in ev.http_consumes
    assert "/health" in ev.http_consumes
    assert "/internal/echo" in ev.http_consumes


def test_nats_publish_subject_extracted() -> None:
    text = '''
    private static final String SUBJECT = "business.push.request";
    natsConnection.publish("business.push.request", body);
    '''
    ev = _extract_from_text(text)
    assert "business.push.request" in ev.nats_emits


def test_nats_subscribe_subject_extracted() -> None:
    text = '''
    @JetStreamListener(subject = "business.push.request",
                       queue = "pos-server-backend-queue")
    public void handle(Message m) { ... }
    natsConnection.subscribe("change.events.sales");
    '''
    ev = _extract_from_text(text)
    assert "business.push.request" in ev.nats_consumes
    assert "change.events.sales" in ev.nats_consumes


def test_mongo_collection_extracted() -> None:
    text = '''
    @Document(collection = "sales")
    public class SalesDao { ... }
    db.getCollection("Parties");
    '''
    ev = _extract_from_text(text)
    assert "sales" in ev.collections
    assert "Parties" in ev.collections


def _extract_from_text(text: str) -> link.RepoEvidence:
    """Helper: run all patterns over a text blob, return a RepoEvidence
    object (no DB needed)."""
    ev = link.RepoEvidence(repo="X")
    for pat in link._HTTP_SERVER:
        for m in pat.finditer(text):
            link._add_path(ev.http_emits, m.group(1))
    for pat in link._HTTP_CLIENT:
        for m in pat.finditer(text):
            link._add_path(ev.http_consumes, m.group(1))
    for pat in link._NATS_PUBLISH:
        for m in pat.finditer(text):
            ev.nats_emits.add(m.group(1).strip())
    for pat in link._NATS_SUBSCRIBE:
        for m in pat.finditer(text):
            ev.nats_consumes.add(m.group(1).strip())
    for pat in link._MONGO_COLLECTION:
        for m in pat.finditer(text):
            ev.collections.add(m.group(1).strip())
    return ev


# ─── Edge fusion ──────────────────────────────────────────────────────

def test_compute_edges_http_pair() -> None:
    a = link.RepoEvidence(
        repo="api", http_emits={"/api/users", "/api/auth"},
    )
    b = link.RepoEvidence(
        repo="web", http_consumes={"/api/users", "/missing"},
    )
    edges = link.compute_edges([a, b])
    http_edges = [e for e in edges if e.via == "http"]
    assert len(http_edges) == 1
    e = http_edges[0]
    assert e.src == "api" and e.dst == "web"
    assert "/api/users" in e.evidence
    assert 0 < e.confidence <= 1.0


def test_compute_edges_nats_pair() -> None:
    a = link.RepoEvidence(repo="prod", nats_emits={"orders.created"})
    b = link.RepoEvidence(repo="cons", nats_consumes={"orders.created"})
    edges = link.compute_edges([a, b])
    nats_edges = [e for e in edges if e.via == "nats"]
    assert len(nats_edges) == 1
    assert nats_edges[0].src == "prod"
    assert nats_edges[0].dst == "cons"


def test_compute_edges_shared_collection_emits_one_direction() -> None:
    a = link.RepoEvidence(repo="alpha", collections={"sales", "Parties"})
    b = link.RepoEvidence(repo="beta", collections={"sales", "x"})
    edges = link.compute_edges([a, b])
    shared = [e for e in edges if e.via == "shared_collection"]
    assert len(shared) == 1
    # alphabetical: alpha < beta, so src=alpha
    assert shared[0].src == "alpha" and shared[0].dst == "beta"
    assert "sales" in shared[0].evidence


def test_compute_edges_no_overlap_no_edge() -> None:
    a = link.RepoEvidence(repo="a", http_emits={"/x"})
    b = link.RepoEvidence(repo="b", http_consumes={"/y"})
    assert link.compute_edges([a, b]) == []


def test_self_pair_skipped() -> None:
    a = link.RepoEvidence(
        repo="solo", http_emits={"/x"}, http_consumes={"/x"},
    )
    assert link.compute_edges([a]) == []


# ─── path normalisation ───────────────────────────────────────────────

def test_add_path_strips_host() -> None:
    s: set[str] = set()
    link._add_path(s, "https://api.example.com/v1/users")
    assert "/v1/users" in s


def test_add_path_prefixes_slash() -> None:
    s: set[str] = set()
    link._add_path(s, "v1/users")
    assert "/v1/users" in s


def test_add_path_drops_too_short() -> None:
    s: set[str] = set()
    link._add_path(s, "/")
    assert s == set()
