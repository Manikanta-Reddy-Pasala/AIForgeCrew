"""Boot persistence for the NUC stack behind tickets.oneshell.in.

After a reboot, user systemd units only start when linger is on and the
units are enabled. deploy.sh used to restart aiforge-api without enabling
it — this pins the scripts that close that gap.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

NUC = Path(__file__).resolve().parents[2] / "scripts" / "runtime" / "nuc"
ENSURE = NUC / "ensure-boot.sh"
DEPLOY = NUC / "deploy.sh"
API_UNIT = NUC / "aiforge-api.service"


def test_ensure_boot_and_deploy_parse_as_bash() -> None:
    for script in (ENSURE, DEPLOY):
        proc = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{script.name}: {proc.stderr}"


def test_ensure_boot_enables_linger_api_docker_and_wireguard() -> None:
    text = ENSURE.read_text(encoding="utf-8")
    assert "enable-linger" in text
    assert "systemctl --user enable aiforge-api.service" in text
    assert "systemctl enable --now docker" in text
    assert "wg-quick@wg0" in text
    # Must not claim success when sudo could not enable boot prerequisites.
    assert 'fail=1' in text or "fail=1" in text
    assert "exit 1" in text
    assert "boot persistence is incomplete" in text


def test_deploy_calls_ensure_boot() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert "ensure-boot.sh" in text
    # Timers are enabled inside ensure-boot now; deploy must not skip boot.
    assert "boot persistence" in text.lower() or "ensure-boot" in text


def test_api_unit_allows_unauth_nonloopback_for_wg_proxy() -> None:
    text = API_UNIT.read_text(encoding="utf-8")
    assert "AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1" in text
    assert "AIFORGE_BIND_HOST=0.0.0.0" in text
    assert "run.sh --host 0.0.0.0" in text
