# tests/python/test_validate_configs.py
import subprocess
import sys
from pathlib import Path


def test_validator_runs_clean_on_current_repo():
    result = subprocess.run(
        [sys.executable, "tools/validate_configs.py"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_validator_fails_on_broken_permissions(tmp_path, monkeypatch):
    # Copy repo to tmp, break a permissions file, run validator, expect failure.
    import shutil
    dst = tmp_path / "repo"
    shutil.copytree(".", dst, ignore=shutil.ignore_patterns(".git", "node_modules", ".venv"))
    broken = dst / "agents/em/permissions.yml"
    broken.write_text("role: ceo\n")  # invalid role
    result = subprocess.run(
        [sys.executable, str(Path("tools/validate_configs.py").resolve())],
        cwd=dst, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "permissions" in (result.stdout + result.stderr).lower()
