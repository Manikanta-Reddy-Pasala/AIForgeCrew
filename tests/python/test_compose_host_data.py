"""Guard: the docker stack must persist ALL data on the HOST (bind mounts),
never in docker-internal named volumes, and mount the agent workspace."""
from __future__ import annotations
import pathlib, yaml

_COMPOSE = pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _c():
    return yaml.safe_load(_COMPOSE.read_text())


def test_no_named_volumes():
    d = _c()
    assert "volumes" not in d or not d["volumes"], "named volumes must be gone"


def test_all_data_services_bind_mount_host():
    svc = _c()["services"]
    # every data path is a host bind (contains a host path, not a bare name)
    pg = " ".join(svc["postgres"]["volumes"])
    assert "AIFORGE_DATA_DIR" in pg and "/var/lib/postgresql/data" in pg
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
