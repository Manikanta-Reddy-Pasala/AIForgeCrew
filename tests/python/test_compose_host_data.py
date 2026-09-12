"""Guard the docker-mode sandbox's boundary (docker-compose.yml + entrypoint).

The expectation it encodes: inside the box the agent has full rights and open
outbound network; of the host it sees exactly ONE folder, ~/.aiforge (settings,
credentials, memory, tickets, workspaces), mounted at the same path. The whole
host filesystem used to be mounted read-write at /host — that is what must
never come back. User data never lives in a docker volume: the only named
volume holds the app copy and what was installed for it, which a reinstall
recreates.
"""
from __future__ import annotations

import pathlib

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "docker-compose.yml"
_ENTRY = (_ROOT / "docker" / "entrypoint.sh").read_text()


def _svc():
    return yaml.safe_load(_COMPOSE.read_text())["services"]["aiforge"]


def test_the_only_host_folder_is_aiforge():
    binds = [v for v in _svc()["volumes"] if not v.startswith("aiforge-state:")]
    assert binds == ["${AIFORGE_CONFIG_DIR:-~/.aiforge}:${AIFORGE_HOME:-/home/aiforge}/.aiforge"]


def test_the_host_filesystem_is_not_mounted():
    vols = " ".join(_svc()["volumes"])
    assert ":/host" not in vols
    assert "AIFORGE_HOST_ROOT" not in vols


def test_the_named_volume_holds_only_reinstallable_state():
    d = yaml.safe_load(_COMPOSE.read_text())
    assert set(d["volumes"]) == {"aiforge-state"}
    assert "aiforge-state:/var/lib/aiforge" in _svc()["volumes"]


def test_the_box_runs_as_the_host_user_with_full_rights():
    args = _svc()["build"]["args"]
    assert args["APP_UID"] == "${AIFORGE_UID:-1000}"
    dockerfile = (_ROOT / "Dockerfile").read_text()
    assert "NOPASSWD:ALL" in dockerfile
    assert "AIFORGE_CHAT_WORKSPACE_JAIL=0" in _ENTRY


def test_your_projects_folder_is_mounted_only_when_asked():
    override = yaml.safe_load((_ROOT / "docker" / "compose.repos.yml").read_text())
    svc = override["services"]["aiforge"]
    assert svc["volumes"] == ["${AIFORGE_REPOS_DIR}:${AIFORGE_REPOS_DIR}"]
    assert svc["environment"]["AIFORGE_REPO_ROOT"] == "${AIFORGE_REPOS_DIR}"
    assert "AIFORGE_REPO_ROOT=${APP_HOME}/.aiforge/repos" in (_ROOT / "Dockerfile").read_text()


def test_the_box_boots_the_same_run_sh_the_host_would():
    assert "AIFORGE_IN_SANDBOX=1" in _ENTRY
    assert "./run.sh ${AIFORGE_RUN_ARGS:-}" in _ENTRY


def test_the_image_installs_no_packages_at_build():
    """Python deps, node, the UI and codegraph come from run.sh on first start
    (lock-pinned, Artifactory-only); the image carries OS packages only."""
    code = "\n".join(ln for ln in (_ROOT / "Dockerfile").read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    for installer in ("pip install", "npm ci", "npm install", "uv pip", "uv sync"):
        assert installer not in code, installer
