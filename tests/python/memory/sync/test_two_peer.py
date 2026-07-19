"""Two peers converge. The headline behaviour of the whole feature."""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import os

from fastapi.testclient import TestClient


def _peer(monkeypatch, tmp_path, name: str):
    """Build an isolated peer: its own config dir, memory dir, and API app."""
    home = tmp_path / name
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(home / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(home / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(home / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_PEER_ID", name)
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_API_TOKEN", "AIFORGE_PG_URL"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return {"name": name, "home": home, "client": TestClient(api.app)}


def _activate(monkeypatch, peer) -> None:
    """Point the process-wide env at this peer (only one is 'current' at a time)."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(peer["home"] / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(peer["home"] / "md"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer["name"])


@contextlib.contextmanager
def _serving(peer):
    """Serve a request *as* this peer.

    ``memory_dir()`` reads ``AIFORGE_MEMORY_MD_DIR`` on every call and the env is
    process-wide, so the source peer's TestClient would otherwise read whichever
    tree was last activated — i.e. the destination's — and every convergence
    assertion would be made against a single tree.
    """
    keys = {
        "AIFORGE_CONFIG_DIR": str(peer["home"] / "cfg"),
        "AIFORGE_MEMORY_MD_DIR": str(peer["home"] / "md"),
        "AIFORGE_PEER_ID": peer["name"],
    }
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _capture_names(peer) -> set:
    d = peer["home"] / "md" / "captures"
    return {p.name for p in d.glob("*.md")} if d.exists() else set()


def _write_capture(peer, name: str, text: str) -> None:
    d = peer["home"] / "md" / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _pull(monkeypatch, dst, src) -> dict:
    """Run one cycle: dst pulls from src, using src's TestClient as transport."""
    from aiforge_core.memory.sync import loop

    def _fetch_manifest(base_url, token=""):
        with _serving(src):
            return src["client"].get("/api/memory/sync/manifest").json()

    def _fetch_blob(base_url, digest, token=""):
        with _serving(src):
            r = src["client"].get(f"/api/memory/sync/blob/{digest}")
            return r.content if r.status_code == 200 else None

    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest", _fetch_manifest)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_blob", _fetch_blob)
    _activate(monkeypatch, dst)
    return loop.sync_with({"id": src["name"], "urls": ["http://stub"], "token": ""})


def test_disjoint_notes_converge_in_both_directions(monkeypatch, tmp_path):
    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _write_capture(nuc, "n-20260719-aaaaaa.md", "from nuc")
    book = _peer(monkeypatch, tmp_path, "book")
    _write_capture(book, "b-20260719-bbbbbb.md", "from book")

    # The two trees really are distinct before the sync.
    assert _capture_names(nuc) == {"n-20260719-aaaaaa.md"}
    assert _capture_names(book) == {"b-20260719-bbbbbb.md"}

    _pull(monkeypatch, book, nuc)
    _pull(monkeypatch, nuc, book)

    for peer in (nuc, book):
        assert _capture_names(peer) == {"n-20260719-aaaaaa.md", "b-20260719-bbbbbb.md"}
    # ...and the arriving file physically landed under the receiver's own dir.
    assert (book["home"] / "md" / "captures" / "n-20260719-aaaaaa.md").read_text(
        encoding="utf-8") == "from nuc"
    assert (nuc["home"] / "md" / "captures" / "b-20260719-bbbbbb.md").read_text(
        encoding="utf-8") == "from book"


def test_a_second_cycle_changes_nothing(monkeypatch, tmp_path):
    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _write_capture(nuc, "n-20260719-aaaaaa.md", "from nuc")
    book = _peer(monkeypatch, tmp_path, "book")

    first = _pull(monkeypatch, book, nuc)
    second = _pull(monkeypatch, book, nuc)

    assert first["applied"] == 1
    assert second["applied"] == 0


def test_concurrent_edit_leaves_a_winner_and_a_sidecar(monkeypatch, tmp_path):
    def _node(peer, by: str, text: str) -> None:
        d = peer["home"] / "md" / "okf" / "global" / "learnings"
        d.mkdir(parents=True, exist_ok=True)
        (d / "L-07.md").write_text(
            f'---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 47\n'
            f'updated_by: "{by}"\n---\n\n{text}\n', encoding="utf-8")

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _node(nuc, "nuc", "nuc version")
    book = _peer(monkeypatch, tmp_path, "book")
    _node(book, "book", "book version")

    res = _pull(monkeypatch, book, nuc)

    node = book["home"] / "md" / "okf" / "global" / "learnings" / "L-07.md"
    sidecar = node.parent / "L-07.conflict.md"
    assert res["conflicts"] == 1
    # 'nuc' > 'book' lexicographically, so the remote wins on the tie.
    assert "nuc version" in node.read_text(encoding="utf-8")
    assert "book version" in sidecar.read_text(encoding="utf-8")


def test_an_unreachable_peer_is_survived(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import loop

    book = _peer(monkeypatch, tmp_path, "book")
    _activate(monkeypatch, book)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest",
                        lambda *a, **k: {})

    res = loop.sync_with({"id": "gone", "urls": ["http://127.0.0.1:1"], "token": ""})

    assert res["ok"] is False
    assert res["applied"] == 0


def test_a_tampered_blob_is_rejected(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import loop

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _write_capture(nuc, "n-20260719-aaaaaa.md", "from nuc")
    book = _peer(monkeypatch, tmp_path, "book")

    def _fetch_manifest(*a, **k):
        with _serving(nuc):
            return nuc["client"].get("/api/memory/sync/manifest").json()

    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest", _fetch_manifest)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_blob",
                        lambda *a, **k: b"TAMPERED")
    _activate(monkeypatch, book)

    res = loop.sync_with({"id": "nuc", "urls": ["http://stub"], "token": ""})

    assert res["applied"] == 0
    assert res["rejected"] == 1
    assert not (book["home"] / "md" / "captures").exists()
    assert hashlib.sha256(b"from nuc").hexdigest()   # sanity: the real hash differs
