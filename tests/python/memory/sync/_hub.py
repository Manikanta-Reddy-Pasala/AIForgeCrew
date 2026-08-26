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

    def _params(group):
        """The group as the real transport sends it: a query parameter."""
        return {"group": group} if group else None

    def _fetch_groups(base_url):
        with serving(admin):
            r = admin["client"].get("/api/memory/sync/groups")
            return None if r.status_code != 200 else r.json().get("groups")

    def _fetch_manifest(base_url, token="", group=""):
        with serving(admin):
            r = admin["client"].get("/api/memory/sync/manifest", params=_params(group))
            return r.json() if r.status_code == 200 else {}

    def _fetch_blob(base_url, digest, token="", group=""):
        with serving(admin):
            r = admin["client"].get(f"/api/memory/sync/blob/{digest}",
                                    params=_params(group))
            return r.content if r.status_code == 200 else None

    def _offer(base_url, entries, group=""):
        from aiforge_core.memory.sync import identity

        peer = identity.self_id()
        with serving(admin):
            r = admin["client"].post("/api/memory/sync/offer",
                                     json={"peer": peer, "group": group,
                                           "entries": entries})
            return None if r.status_code != 200 else r.json().get("want")

    def _push_blob(base_url, entry, body, group=""):
        from aiforge_core.memory.sync import identity

        payload = {"peer": identity.self_id(), "group": group,
                   "entry": json.loads(json.dumps(entry)),
                   "body": base64.b64encode(body).decode()}
        with serving(admin):
            r = admin["client"].post("/api/memory/sync/push", json=payload)
            return bool(r.status_code == 200 and r.json().get("applied"))

    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_groups", _fetch_groups)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest", _fetch_manifest)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_blob", _fetch_blob)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.offer", _offer)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.push_blob", _push_blob)


def cycle(monkeypatch, spoke, admin, *, group: str = "") -> dict:
    """One full cycle for ``spoke`` against ``admin``: push, then pull."""
    from aiforge_core.memory.sync import loop

    wire(monkeypatch, admin)
    activate(monkeypatch, spoke)
    return loop.sync_with(spoke["admin_url"] or "http://admin", group=group)


def run_once(monkeypatch, spoke, admin) -> list:
    """A full ``loop.run_once``: group discovery, resolution, then the cycle.

    Use this rather than ``cycle`` when the test is about the GROUP — ``cycle``
    is handed a group and skips the resolution that decides it.
    """
    from aiforge_core.memory.sync import loop

    wire(monkeypatch, admin)
    activate(monkeypatch, spoke)
    return loop.run_once()


def author(machine, key: str, body: str, *, rev: int = 1, scope: str = "") -> None:
    """Write one authored node into this machine's own ``okf/``.

    The body carries a file reference on purpose: the outbound filter
    (``sync.redact``) holds back a node with no project signal at all, so a
    fixture saying only "hello" would be filtered and every convergence
    assertion would fail for a reason unrelated to what is under test.
    """
    from aiforge_core.memory.sync import _io

    okf = _io.root() / "okf"
    okf.mkdir(parents=True, exist_ok=True)
    scope_line = f'scope: "{scope}"\n' if scope else ""
    (okf / f"{key}.md").write_text(
        f'---\ntype: learning\nid: "{key}"\norigin: "{machine["name"]}"\n'
        f'rev: {rev}\nupdated_by: "{machine["name"]}"\n{scope_line}---\n\n'
        f"{body}\n\nSee `aiforge_core/memory/sync/loop.py` — `run_once()`.\n",
        encoding="utf-8")


def write_capture(machine, name: str, text: str) -> None:
    d = machine["home"] / "md" / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def capture_names(machine) -> set:
    d = machine["home"] / "md" / "captures"
    return {p.name for p in d.glob("*.md")} if d.exists() else set()


__all__ = ["node", "activate", "serving", "wire", "cycle", "run_once", "author",
           "write_capture", "capture_names"]
