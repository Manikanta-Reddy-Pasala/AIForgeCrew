"""Guard: the docker stack must persist ALL data on the HOST (bind mounts),
never in docker-internal named volumes, and mount the agent workspace."""
from __future__ import annotations
import pathlib, yaml

_COMPOSE = pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _c():
    return yaml.safe_load(_COMPOSE.read_text())


def test_only_postgres_uses_named_volume():
    # Postgres is the ONE exception (host bind breaks initdb on macOS/virtiofs);
    # everything else is a host bind. No other named volumes allowed.
    d = _c()
    named = set((d.get("volumes") or {}).keys())
    assert named == {"aiforge_pgdata"}, f"unexpected named volumes: {named}"


def test_data_services_bind_mount_host():
    svc = _c()["services"]
    # postgres persists via the named volume; neo4j is a host bind
    assert "aiforge_pgdata:/var/lib/postgresql/data" in " ".join(svc["postgres"]["volumes"])
    neo = " ".join(svc["neo4j"]["volumes"])
    assert ":/data" in neo and ("NEO4J_DATA_DIR" in neo or "/data/neo4j" in neo)


def test_api_and_runner_mount_workspace_and_repo_root():
    svc = _c()["services"]
    for s in ("api", "runner"):
        vols = " ".join(svc[s]["volumes"])
        assert "/workspace" in vols, f"{s} missing workspace mount"
        assert "/data/aiforge" in vols, f"{s} missing host app-state"
    # AIFORGE_REPO_ROOT points at the mounted workspace
    assert svc["api"]["environment"]["AIFORGE_REPO_ROOT"] == "/workspace"
