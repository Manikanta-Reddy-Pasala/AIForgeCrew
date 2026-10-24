"""`./run.sh --isolated`: the egress allowlist enforced by the NETWORK.

By default the box runs `network_mode: host` — it shares this machine's network
stack, so ``net/egress.py`` is the only thing between the agent and the
internet, and it only governs DECLARED destinations and page fetches. A `curl`
typed into the agent's shell never passes through it.

`--isolated` puts the box on an internal docker network with no route out and
sends every outbound request through a proxy holding the operator's allowlist.
These pin the wiring: the right base file, a DEFAULT-DENY proxy config, the
allowlist written from the same env the app reads, and a refusal to combine two
base files.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUN_SH = REPO / "run.sh"


def _run(tmp_path: Path, args: list[str], extra_env: dict | None = None):
    """run.sh with a fake `docker` on PATH that records how it was called."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    fake = bindir / "docker"
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
    fake.chmod(0o755)

    dst = tmp_path / "run.sh"
    shutil.copy(RUN_SH, dst)
    env = {**os.environ,
           "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
           "AIFORGE_CONFIG_DIR": str(tmp_path / "cfg"),
           # Force the sandbox branch: this suite runs INSIDE a container, where
           # detection would otherwise take the in-box path and never call docker.
           "AIFORGE_IN_SANDBOX": "0"}
    env.pop("COMPOSE_FILE", None)
    env.update(extra_env or {})
    proc = subprocess.run(["bash", str(dst), *args], cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=120, env=env)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def _sandbox(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / ".sandbox"


def test_isolated_selects_the_isolated_base_file(tmp_path: Path):
    """It REPLACES the base rather than overlaying: compose cannot take
    `network_mode: host` back off once the default file has set it."""
    _proc, calls = _run(tmp_path, ["--isolated"],
                        {"AIFORGE_EGRESS_ALLOW_HOSTS": "artifactory.internal"})
    up = [c for c in calls if " up " in f" {c} "]
    assert up, calls
    assert "-f docker-compose.isolated.yml" in up[0]
    assert "-f docker-compose.yml" not in up[0]


def test_the_default_run_is_unchanged(tmp_path: Path):
    _proc, calls = _run(tmp_path, [])
    up = [c for c in calls if " up " in f" {c} "]
    assert up, calls
    assert "docker-compose.isolated.yml" not in up[0]


def test_nothing_about_the_boundary_is_written_where_the_box_can_write(tmp_path: Path):
    """~/.aiforge is mounted read-write into the box, and the agent has full
    rights there: a proxy config or allow file in it is one the agent edits.
    A stale one from an older run.sh is removed."""
    sb = _sandbox(tmp_path)
    sb.mkdir(parents=True)
    (sb / "allow.txt").write_text(".*\n")
    (sb / "tinyproxy.conf").write_text("FilterDefaultDeny No\n")
    _run(tmp_path, ["--isolated"], {"AIFORGE_EGRESS_ALLOW_HOSTS": "artifactory.internal"})
    assert not (sb / "allow.txt").exists() and not (sb / "tinyproxy.conf").exists()


def test_the_proxy_config_is_static_and_denies_by_default():
    """An allowlist that defaults to ALLOW is not an allowlist."""
    conf = (REPO / "docker" / "tinyproxy.conf").read_text()
    assert "FilterDefaultDeny Yes" in conf and "FilterExtended On" in conf
    assert 'Filter "/policy/allow.txt"' in conf
    assert "ConnectPort 443" in conf
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    assert "./docker/tinyproxy.conf:/etc/tinyproxy/tinyproxy.conf:ro" in compose
    assert "egress-policy:/policy:ro" in compose          # the proxy only reads it


def test_the_allowlist_owner_is_off_the_box_network():
    """The controller is on `out` only — the box has no route to it — and the
    UI front door refuses the box's subnet on its route."""
    import re
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    ctl = compose[compose.index("  egress-ctl:"):compose.index("  model:")]
    assert "networks: [out]" in ctl and "box" not in ctl.split("networks:")[1].split("\n")[0]
    assert re.search(r"subnet: \$\{AIFORGE_BOX_SUBNET:-[0-9./]+\}", compose)
    ui = (REPO / "docker" / "ui-proxy.conf.template").read_text()
    loc = ui[ui.index("location /egress-admin/"):]
    assert "deny ${AIFORGE_BOX_SUBNET};" in loc.split("}")[0]
    assert "AIFORGE_BOX_SUBNET=${AIFORGE_BOX_SUBNET:-" in compose     # nginx gets the same value


def test_the_model_goes_through_its_own_relay(tmp_path: Path):
    """The model client does not use the proxy; it reaches a relay on the box
    network that forwards to the real endpoint only. A loopback endpoint is
    this machine's: the relay reaches it as host.docker.internal."""
    proc, calls = _run(tmp_path, ["--isolated"],
                       {"AIFORGE_LM_BASE_URL": "http://127.0.0.1:1234/v1"})
    out = proc.stdout + proc.stderr
    assert "model via relay -> host.docker.internal:1234" in out
    proc, _ = _run(tmp_path, ["--isolated"],
                   {"AIFORGE_LM_BASE_URL": "http://192.168.70.185:8081/v1"})
    assert "model via relay -> 192.168.70.185:8081" in proc.stdout + proc.stderr
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    assert "AIFORGE_LM_BASE_URL=${AIFORGE_BOX_LM_URL" in compose
    assert "host.docker.internal:host-gateway" not in compose.split("  egress:")[0]


def test_the_ui_is_published_through_its_own_proxy():
    """A container on an internal network cannot publish a port itself, so the
    UI is served by an nginx that sits on both networks.

    Its config varies with nothing, so it is a file in the repo rather than
    something run.sh echoes out at start-up — compose mounts it directly."""
    conf = (REPO / "docker" / "ui-proxy.conf.template").read_text()
    assert "proxy_pass http://aiforge:8799;" in conf
    # SSE: the chat streams, so buffering would hold every token to the end.
    assert "proxy_buffering off;" in conf


def test_compose_mounts_that_static_proxy_config():
    """The file is only useful if the ui container actually gets it."""
    compose = (REPO / "docker-compose.isolated.yml").read_text()
    assert "./docker/ui-proxy.conf.template:/etc/nginx/templates/default.conf.template:ro" in compose


def test_isolated_refuses_to_fight_a_site_compose_file(tmp_path: Path):
    proc, _calls = _run(tmp_path, ["--isolated"],
                        {"COMPOSE_FILE": "/tmp/site.yml"})
    assert proc.returncode != 0
    assert "cannot be combined" in (proc.stderr + proc.stdout)
