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
           "AIFORGE_MODE": "docker"}
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


def test_the_allowlist_comes_from_the_same_env_the_app_reads(tmp_path: Path):
    """Settings and the network must never disagree about what is allowed."""
    _run(tmp_path, ["--isolated"],
         {"AIFORGE_EGRESS_ALLOW_HOSTS": "artifactory.internal, jira.corp"})
    allow = (_sandbox(tmp_path) / "allow.txt").read_text().split()
    assert allow == ["artifactory.internal", "jira.corp"]


def test_the_proxy_denies_by_default(tmp_path: Path):
    """An allowlist that defaults to ALLOW is not an allowlist."""
    _run(tmp_path, ["--isolated"],
         {"AIFORGE_EGRESS_ALLOW_HOSTS": "artifactory.internal"})
    conf = (_sandbox(tmp_path) / "tinyproxy.conf").read_text()
    assert "FilterDefaultDeny Yes" in conf
    assert "/etc/tinyproxy/allow.txt" in conf


def test_an_empty_allowlist_still_writes_a_file(tmp_path: Path):
    """The proxy mounts it; a missing file is a container that will not start,
    which reads as "isolation is broken" rather than "nothing is allowed"."""
    _run(tmp_path, ["--isolated"], {"AIFORGE_EGRESS_ALLOW_HOSTS": ""})
    assert (_sandbox(tmp_path) / "allow.txt").exists()


def test_the_ui_is_published_through_its_own_proxy(tmp_path: Path):
    """A container on an internal network cannot publish a port itself."""
    _run(tmp_path, ["--isolated"], {"AIFORGE_EGRESS_ALLOW_HOSTS": "x.internal"})
    conf = (_sandbox(tmp_path) / "ui-proxy.conf").read_text()
    assert "proxy_pass http://aiforge:8799;" in conf
    # SSE: the chat streams, so buffering would hold every token to the end.
    assert "proxy_buffering off;" in conf


def test_isolated_refuses_to_fight_a_site_compose_file(tmp_path: Path):
    proc, _calls = _run(tmp_path, ["--isolated"],
                        {"COMPOSE_FILE": "/tmp/site.yml"})
    assert proc.returncode != 0
    assert "cannot be combined" in (proc.stderr + proc.stdout)
