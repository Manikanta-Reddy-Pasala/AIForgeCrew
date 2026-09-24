"""Boot persistence + passwordless system boot for the NUC."""
from __future__ import annotations

import subprocess
from pathlib import Path

NUC = Path(__file__).resolve().parents[2] / "scripts" / "runtime" / "nuc"
ENSURE = NUC / "ensure-boot.sh"
INSTALL = NUC / "install-system-boot.sh"
DEPLOY = NUC / "deploy.sh"
API_UNIT = NUC / "aiforge-api.service"
SYSTEM_IN = NUC / "aiforge-api.system.service.in"


def test_boot_scripts_parse_as_bash() -> None:
    for script in (ENSURE, INSTALL, DEPLOY):
        proc = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{script.name}: {proc.stderr}"


def test_ensure_boot_prefers_system_unit_and_fails_hard() -> None:
    text = ENSURE.read_text(encoding="utf-8")
    assert "install-system-boot.sh" in text
    assert "/etc/systemd/system/aiforge-api.service" in text
    assert "fail=1" in text
    assert "exit 1" in text
    assert "wg1" in text  # NUC ships wg1.conf
    assert ".conf missing" in text
    docker_at = text.index("docker at boot")
    api_at = text.index("enable AIForge at boot")
    assert docker_at < api_at


def test_install_system_boot_nopasswd_and_autologin() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "/etc/sudoers.d/aiforge-boot" in text
    assert "NOPASSWD" in text
    assert "visudo -cf" in text
    assert "Cmnd_Alias AIFORGE_BOOT" in text
    assert "enable --now docker" in text
    assert "wg-quick@wg1" in text
    assert "ensure-boot.sh, \\" not in text
    assert "deploy-nuc.sh" not in text.split("visudo")[0]
    assert "AutomaticLoginEnable" in text or "autologin-user" in text
    assert "systemctl --user -M" in text or "XDG_RUNTIME_DIR" in text
    assert "nuc-registry.conf" in text
    assert "does not overwrite" in text
    unit = SYSTEM_IN.read_text(encoding="utf-8")
    assert "multi-user.target" in unit
    assert "TimeoutStartSec=2400" in unit
    assert "AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1" in unit
    drop = (NUC / "nuc-registry.conf").read_text(encoding="utf-8")
    assert "pypi.org" in drop
    assert "TimeoutStartSec=2400" in drop


def test_deploy_calls_ensure_boot() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert "ensure-boot.sh" in text


def test_api_unit_allows_unauth_nonloopback_for_wg_proxy() -> None:
    text = API_UNIT.read_text(encoding="utf-8")
    assert "AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1" in text
    assert "AIFORGE_BIND_HOST=0.0.0.0" in text
