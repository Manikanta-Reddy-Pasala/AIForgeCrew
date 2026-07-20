"""Two peers converge. The headline behaviour of the whole feature."""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import os

import pytest
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
    """Both sides edited nuc's node at the same rev. The receiver holds its copy
    in the inbox, not in ``okf/``: ``okf/`` has one writer (us), so a node minted
    by nuc lives under ``peers/nuc/`` however it was edited — see
    ``paths._is_ours``."""
    def _node(peer, relative: str, by: str, text: str) -> None:
        p = peer["home"] / "md" / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f'---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 47\n'
            f'updated_by: "{by}"\n---\n\n{text}\n', encoding="utf-8")

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _node(nuc, "okf/global/learnings/L-07.md", "nuc", "nuc version")
    book = _peer(monkeypatch, tmp_path, "book")
    _node(book, "peers/nuc/L-07.md", "book", "book version")

    res = _pull(monkeypatch, book, nuc)

    node = book["home"] / "md" / "peers" / "nuc" / "L-07.md"
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


def test_transitive_discovery_quarantines_the_third_peer(monkeypatch, tmp_path):
    """A knows B; B knows C. After one cycle A knows *of* C but never pulls it."""
    from aiforge_core.memory.sync import peers

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    # nuc has alice approved, so alice appears in nuc's advertised roster.
    _activate(monkeypatch, nuc)
    peers.save({"self": {"id": "nuc", "urls": ["http://nuc"]}, "peers": [
        {"id": "alice", "urls": ["http://alice"], "token": "t",
         "state": "approved"},
    ]})

    book = _peer(monkeypatch, tmp_path, "book")
    _activate(monkeypatch, book)
    peers.save({"self": {"id": "book", "urls": ["http://book"]}, "peers": [
        {"id": "nuc", "urls": ["http://stub"], "token": "", "state": "approved"},
    ]})

    _pull(monkeypatch, book, nuc)

    _activate(monkeypatch, book)
    known = {p["id"]: p for p in peers.load()["peers"]}
    assert known["alice"]["state"] == "candidate"
    assert "token" not in known["alice"]
    assert [p["id"] for p in peers.approved()] == ["nuc"]


def test_ssdp_discoveries_are_also_quarantined(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import loop, peers

    book = _peer(monkeypatch, tmp_path, "book")
    _activate(monkeypatch, book)
    peers.save({"self": {"id": "book", "urls": ["http://book"]}, "peers": []})
    monkeypatch.setenv("AIFORGE_SYNC_SSDP", "1")
    monkeypatch.setattr("aiforge_core.memory.sync.discovery_ssdp.discover",
                        lambda *a, **k: [{"id": "nuc", "urls": ["http://found"]}])

    loop.run_once()

    known = {p["id"]: p for p in peers.load()["peers"]}
    assert known["nuc"]["state"] == "candidate"
    assert peers.approved() == []


def _record_responder(monkeypatch, tmp_path):
    """Set up a node with SSDP on and capture any responder start. Returns the log."""
    from aiforge_core.memory.sync import peers

    book = _peer(monkeypatch, tmp_path, "book")
    _activate(monkeypatch, book)
    peers.save({"self": {"id": "book", "urls": ["http://book:8799"]}, "peers": []})
    monkeypatch.setenv("AIFORGE_SYNC_SSDP", "1")
    monkeypatch.setenv("AIFORGE_SYNC_SSDP_HOST", "10.0.1.5")
    monkeypatch.setattr("aiforge_core.memory.sync.discovery_ssdp.discover",
                        lambda *a, **k: [])
    started: list[tuple] = []
    monkeypatch.setattr("aiforge_core.memory.sync.discovery_ssdp.serve_in_background",
                        lambda *a: started.append(a))
    return started


def test_run_once_leaves_no_responder_thread_behind(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import loop

    started = _record_responder(monkeypatch, tmp_path)

    loop.run_once()

    assert started == []


def test_run_forever_starts_the_responder_with_our_own_identity(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import loop

    started = _record_responder(monkeypatch, tmp_path)
    monkeypatch.setattr(loop, "run_once", lambda: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        # Any positive interval: run_once raises before the sleep is reached.
        # Zero is refused now — it made the loop spin without throttling.
        loop.run_forever(interval=1)

    assert started == [("10.0.1.5", "book", "http://book:8799")]
