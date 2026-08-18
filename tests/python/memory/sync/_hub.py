"""Shared harness: build isolated machines and run a real cycle between them.

Every module here drives the actual protocol — ``push.run_once`` and
``loop._pull`` against the other machine's real FastAPI routes — rather than a
hand-rolled stand-in, so a change to the wire format fails the tests instead of
passing them.
"""
from __future__ import annotations

import contextlib
import importlib
import os

from fastapi.testclient import TestClient


def node(monkeypatch, tmp_path, name: str, *, admin_url: str = ""):
    """An isolated machine: its own config dir, memory dir and API app.

    ``admin_url`` empty means this machine IS the admin — the same rule the
    product uses, so a test never has to state a role twice.
    """
    home = tmp_path / name
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(home / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(home / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(home / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_PEER_ID", name)
    if admin_url:
        monkeypatch.setenv("AIFORGE_ADMIN_URL", admin_url)
    else:
        monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_API_TOKEN", "AIFORGE_PG_URL",
              "AIFORGE_ROLE", "AIFORGE_ADMIN_ID"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return {"name": name, "home": home, "client": TestClient(api.app),
            "admin_url": admin_url}


def activate(monkeypatch, machine) -> None:
    """Point the process-wide env at this machine (only one is 'current')."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(machine["home"] / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(machine["home"] / "md"))
    monkeypatch.setenv("AIFORGE_PEER_ID", machine["name"])
    if machine["admin_url"]:
        monkeypatch.setenv("AIFORGE_ADMIN_URL", machine["admin_url"])
    else:
        monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)


@contextlib.contextmanager
def serving(machine):
    """Serve a request *as* this machine.

    ``memory_dir()`` reads ``AIFORGE_MEMORY_MD_DIR`` on every call and the env is
    process-wide, so the admin's TestClient would otherwise read whichever tree
    was last activated — i.e. the spoke's — and every convergence assertion would
    be made against a single tree.
    """
    keys = {
        "AIFORGE_CONFIG_DIR": str(machine["home"] / "cfg"),
        "AIFORGE_MEMORY_MD_DIR": str(machine["home"] / "md"),
        "AIFORGE_PEER_ID": machine["name"],
    }
    saved = {k: os.environ.get(k) for k in keys}
    admin_saved = os.environ.get("AIFORGE_ADMIN_URL")
    os.environ.update(keys)
    if machine["admin_url"]:
        os.environ["AIFORGE_ADMIN_URL"] = machine["admin_url"]
    else:
        os.environ.pop("AIFORGE_ADMIN_URL", None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if admin_saved is None:
            os.environ.pop("AIFORGE_ADMIN_URL", None)
        else:
            os.environ["AIFORGE_ADMIN_URL"] = admin_saved


def wire(monkeypatch, admin) -> None:
    """Route every transport call to ``admin``'s real routes."""
    import base64
    import json

    def _fetch_manifest(base_url, token=""):
        with serving(admin):
            return admin["client"].get("/api/memory/sync/manifest").json()

    def _fetch_blob(base_url, digest, token=""):
        with serving(admin):
            r = admin["client"].get(f"/api/memory/sync/blob/{digest}")
            return r.content if r.status_code == 200 else None

    def _offer(base_url, entries):
        from aiforge_core.memory.sync import identity

        peer = identity.self_id()
        with serving(admin):
            r = admin["client"].post("/api/memory/sync/offer",
                                     json={"peer": peer, "entries": entries})
            return None if r.status_code != 200 else r.json().get("want")

    def _push_blob(base_url, entry, body):
        from aiforge_core.memory.sync import identity

        payload = {"peer": identity.self_id(), "entry": json.loads(json.dumps(entry)),
                   "body": base64.b64encode(body).decode()}
        with serving(admin):
            r = admin["client"].post("/api/memory/sync/push", json=payload)
            return bool(r.status_code == 200 and r.json().get("applied"))

    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest", _fetch_manifest)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_blob", _fetch_blob)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.offer", _offer)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.push_blob", _push_blob)


def cycle(monkeypatch, spoke, admin) -> dict:
    """One full cycle for ``spoke`` against ``admin``: push, then pull."""
    from aiforge_core.memory.sync import loop

    wire(monkeypatch, admin)
    activate(monkeypatch, spoke)
    return loop.sync_with(spoke["admin_url"] or "http://admin")


def write_capture(machine, name: str, text: str) -> None:
    d = machine["home"] / "md" / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def capture_names(machine) -> set:
    d = machine["home"] / "md" / "captures"
    return {p.name for p in d.glob("*.md")} if d.exists() else set()


__all__ = ["node", "activate", "serving", "wire", "cycle", "write_capture",
           "capture_names"]
