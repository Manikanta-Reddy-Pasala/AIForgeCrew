"""L16 — graphify-out browser API.

Covers the four routes added for the operator-facing "Graph" tab:

    GET /api/graphify
    GET /api/graphify/{repo}/report
    GET /api/graphify/{repo}/graph
    GET /graphify/{repo}/wiki/{path}

The Neo4j-backed routes in server.py are NOT exercised here — those are
covered by their own integration tests. We monkeypatch
`_graphify_index` so each test owns its repo whitelist via tmp_path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Skip cleanly on CI hosts that didn't install the [ui] extra.
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aiforge_memory.ui import server as ui_server  # noqa: E402


def _make_repo_with_graphify(tmp_path: Path, repo_name: str, *,
                             include_report: bool = True,
                             include_graph: bool = True,
                             include_wiki: bool = True,
                             node_count: int = 3) -> Path:
    """Lay down a fake repo at tmp_path/<repo_name>/ with a graphify-out
    subtree populated according to flags. Returns the repo root."""
    repo = tmp_path / repo_name
    gout = repo / "graphify-out"
    gout.mkdir(parents=True)
    if include_report:
        (gout / "GRAPH_REPORT.md").write_text(
            f"# {repo_name} graph report\n\nGod nodes: A, B, C\n",
        )
    if include_graph:
        nodes = [{"id": f"n{i}", "label": f"node {i}"} for i in range(node_count)]
        (gout / "graph.json").write_text(json.dumps({
            "directed": True, "nodes": nodes, "links": [],
        }))
    if include_wiki:
        wiki = gout / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text(f"# {repo_name} wiki index\n")
        (wiki / "topic.md").write_text("# Topic\n\nSome content.\n")
    return repo


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with a controllable graphify index.

    The fixture installs three fake repos:
      - alpha:   full graphify-out (report + json + wiki)
      - bravo:   only GRAPH_REPORT.md
      - charlie: NO graphify-out (must NOT appear in /api/graphify)

    Tests can override the index via `_index_override` if they need.
    """
    _make_repo_with_graphify(tmp_path, "alpha", node_count=12)
    _make_repo_with_graphify(
        tmp_path, "bravo",
        include_graph=False, include_wiki=False,
    )
    # `charlie` exists but has no graphify-out
    (tmp_path / "charlie").mkdir()

    fake_index = {
        name: ui_server._graphify_metadata(name, tmp_path / name)
        for name in ("alpha", "bravo")
        if (tmp_path / name / "graphify-out").is_dir()
    }
    # Strip Nones (defensive).
    fake_index = {k: v for k, v in fake_index.items() if v}

    monkeypatch.setattr(ui_server, "_graphify_index", lambda: fake_index)

    app = ui_server.build_app()
    return TestClient(app)


def test_graphify_list_returns_descriptors_with_metadata(client: TestClient) -> None:
    r = client.get("/api/graphify")
    assert r.status_code == 200
    rows = r.json()
    names = sorted(row["repo"] for row in rows)
    assert names == ["alpha", "bravo"]

    alpha = next(row for row in rows if row["repo"] == "alpha")
    assert alpha["has_report"] is True
    assert alpha["has_graph_json"] is True
    assert alpha["has_wiki"] is True
    assert alpha["node_count"] == 12
    assert alpha["size_kb"] >= 0
    assert alpha["updated_at"]            # non-empty ISO string
    assert alpha["path"].endswith("/alpha/graphify-out")


def test_graphify_list_skips_repos_without_graphify_out(client: TestClient) -> None:
    rows = client.get("/api/graphify").json()
    assert all(row["repo"] != "charlie" for row in rows)
    bravo = next(row for row in rows if row["repo"] == "bravo")
    # bravo has only GRAPH_REPORT.md — graph.json + wiki absent
    assert bravo["has_report"] is True
    assert bravo["has_graph_json"] is False
    assert bravo["has_wiki"] is False
    assert bravo["node_count"] == 0


def test_graphify_report_200_for_known_repo_404_for_unknown(client: TestClient) -> None:
    ok = client.get("/api/graphify/alpha/report")
    assert ok.status_code == 200
    assert "alpha graph report" in ok.text
    assert ok.headers["content-type"].startswith("text/markdown")

    missing = client.get("/api/graphify/zeta/report")
    assert missing.status_code == 404


def test_graphify_wiki_rejects_path_traversal(client: TestClient) -> None:
    # Plain ../ — Starlette normalises before routing, so the request
    # never reaches our handler. That's an acceptable fail-closed
    # outcome (any non-200 means the file wasn't served).
    plain = client.get("/graphify/alpha/wiki/../../etc/passwd")
    assert plain.status_code != 200
    assert plain.status_code in (400, 404)

    # URL-encoded `..` does reach the handler — this is the path that
    # actually exercises our _resolve_graphify_path() defense. It must
    # produce 400 ("invalid path"), NOT 200.
    encoded = client.get("/graphify/alpha/wiki/%2E%2E/%2E%2E/etc/passwd")
    assert encoded.status_code == 400, \
        f"traversal not rejected: {encoded.status_code} {encoded.text}"

    # And the unknown-repo guard still kicks in for valid-looking paths.
    unknown = client.get("/graphify/zeta/wiki/index.md")
    assert unknown.status_code == 404

    # Sanity: a plain wiki file under wiki/ resolves fine.
    ok = client.get("/graphify/alpha/wiki/topic.md")
    assert ok.status_code == 200
    assert "Some content" in ok.text


def test_graphify_graph_returns_json(client: TestClient) -> None:
    r = client.get("/api/graphify/alpha/graph")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["directed"] is True
    assert len(body["nodes"]) == 12
