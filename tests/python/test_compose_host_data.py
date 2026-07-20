"""Guard: the docker stack must persist ALL data on the HOST (bind mounts),
never in docker-internal named volumes, and mount the agent workspace.

The stack is single-mode now (embedded SQLite + scoped-OKR memory): ONE
``aiforge`` service, no Postgres/Neo4j/runner sidecars — so the old
per-sidecar assertions were rewritten against the service that exists.
"""
from __future__ import annotations
import pathlib, yaml

_COMPOSE = pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _c():
    return yaml.safe_load(_COMPOSE.read_text())


def test_no_named_volumes():
    # Named volumes hide state inside docker; every mount must be a host bind
    # so `docker compose down -v` / a rebuild can't wipe the user's data.
    d = _c()
    named = set((d.get("volumes") or {}).keys())
    assert not named, f"unexpected named volumes: {named}"


def test_app_state_bind_mounts_host():
    # config + sqlite + memory briefs/captures + caches all land under a host
    # dir (AIFORGE_DATA_DIR) so they survive image rebuilds.
    vols = " ".join(_c()["services"]["aiforge"]["volumes"])
    assert "AIFORGE_DATA_DIR" in vols and ":/data/aiforge" in vols
    assert not any(v.startswith("aiforge_") for v in
                   _c()["services"]["aiforge"]["volumes"])


def test_service_mounts_workspace_and_repo_root():
    svc = _c()["services"]["aiforge"]
    vols = " ".join(svc["volumes"])
    # the agent's workspace is the mounted host filesystem at /host
    assert ":/host" in vols and "AIFORGE_HOST_ROOT" in vols
    # AIFORGE_REPO_ROOT points at that mount
    assert svc["environment"]["AIFORGE_REPO_ROOT"] == "${AIFORGE_REPO_ROOT:-/host}"
