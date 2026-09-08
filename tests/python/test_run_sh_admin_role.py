"""run.sh's memory-role branch: claiming it, giving it up, and refusing it.

The role is the one run.sh setting that is *written back* to disk, and the
reason is severe: a machine that stops being the admin retires its own mesh
fold, so a restart that silently demoted the admin would delete the fleet's
merged knowledge and propagate tombstones to every spoke. These tests run the
real script for the refusal branches (which exit before any bootstrap) and
exercise the shipped writer function itself for the persistence half.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

RUN_SH = Path(__file__).resolve().parents[2] / "run.sh"


def _run(tmp_path: Path, args: list[str], extra_env: dict | None = None):
    """Run a copy of run.sh in an empty dir, so nothing touches the real repo."""
    import os

    dst = tmp_path / "run.sh"
    shutil.copy(RUN_SH, dst)
    env = dict(os.environ)
    env["AIFORGE_CONFIG_DIR"] = str(tmp_path / "cfg")
    env.pop("AIFORGE_ADMIN_URL", None)
    env.pop("AIFORGE_ROLE", None)
    env.update(extra_env or {})
    return subprocess.run(["bash", str(dst), *args], cwd=str(tmp_path),
                          capture_output=True, text=True, env=env, timeout=60)


# ── the refusals ──────────────────────────────────────────────────────────

def test_admin_is_refused_when_a_url_says_this_box_is_a_spoke(tmp_path: Path):
    """--admin used to mean only "open the /admin page", so an operator on a
    spoke may type it out of habit. Promoting that machine would give the fleet
    two admins, both stamping ``derived: mesh``."""
    proc = _run(tmp_path, ["--admin"],
                {"AIFORGE_ADMIN_URL": "http://rig:8799"})

    assert proc.returncode == 2
    assert "cannot be both" in proc.stderr
    assert "--admin-page" in proc.stderr        # …and says what to type instead


def test_admin_is_refused_in_docker_mode(tmp_path: Path):
    """The container never runs the sync loop, so a role claimed there would be
    a statement about a process that does not exist."""
    proc = _run(tmp_path, ["--docker", "--admin"])

    assert proc.returncode == 2
    assert "docker" in proc.stderr.lower()


def test_admin_and_spoke_together_are_refused(tmp_path: Path):
    proc = _run(tmp_path, ["--admin", "--spoke"])

    assert proc.returncode == 2
    assert "opposites" in proc.stderr


def test_help_lists_both_role_flags(tmp_path: Path):
    proc = _run(tmp_path, ["--help"])

    assert proc.returncode == 0
    assert "--admin " in proc.stdout
    assert "--admin-page" in proc.stdout
    assert "--spoke" in proc.stdout


# ── nothing is persisted any more ─────────────────────────────────────────
# The env file is FIXED and committed, so run.sh reads it and never writes to
# it. That removes the drift a self-editing script causes — and puts a real
# obligation on the operator, because a machine that stops being the admin
# retires its own merged fold and tombstones it to every spoke. So the flag
# claims the role for one run and says, loudly, what has to be in the
# environment for it to survive a restart.

def test_the_script_never_writes_to_the_env_file(tmp_path: Path):
    env = tmp_path / "aiforge.env"
    env.write_text("AIFORGE_LM_BASE_URL=http://127.0.0.1:1234/v1\n")
    before = env.read_text()

    _run(tmp_path, ["--admin"])

    assert env.read_text() == before, "run.sh edited its own configuration"
    assert not (tmp_path / ".env").exists(), "run.sh created a .env"
    assert not (tmp_path / "aiforge.env.tmp").exists()


def test_admin_warns_that_the_role_will_not_survive_a_restart(tmp_path: Path):
    r = _run(tmp_path, ["--admin"])

    assert "THIS RUN ONLY" in r.stderr
    assert "RETIRES" in r.stderr                  # names the actual consequence
    assert "AIFORGE_ROLE=admin" in r.stderr       # …and the line that prevents it


def test_no_warning_when_the_environment_already_carries_the_role(tmp_path: Path):
    r = _run(tmp_path, ["--admin"], {"AIFORGE_ROLE": "admin"})

    assert "THIS RUN ONLY" not in r.stderr


def test_spoke_says_where_the_role_has_to_be_removed_from(tmp_path: Path):
    r = _run(tmp_path, ["--spoke"], {"AIFORGE_ROLE": "admin"})

    assert "NOT the admin for this run" in r.stdout
    assert "AIFORGE_ROLE=admin" in r.stdout


def test_the_environment_overrides_the_file(tmp_path: Path):
    """Per-box settings come from the environment; the file is what is the same
    everywhere. `set -a; . file` would have clobbered the environment instead."""
    (tmp_path / "aiforge.env").write_text("AIFORGE_ADMIN_URL=http://from-file:8799\n")

    r = _run(tmp_path, [], {"AIFORGE_ADMIN_URL": "http://from-env:8799"})

    assert "from-env" in r.stdout
    assert "from-file" not in r.stdout


def test_a_value_in_the_file_is_data_not_a_command(tmp_path: Path):
    """The file is parsed, not sourced — a committed file is still not a script."""
    marker = tmp_path / "pwned"
    (tmp_path / "aiforge.env").write_text(
        f'AIFORGE_SYNC_GROUP=$(touch {marker})\n')

    _run(tmp_path, [])

    assert not marker.exists(), "a value in the env file was executed"


def test_a_windows_edited_file_still_parses(tmp_path: Path):
    """WSL operators edit this in Notepad; a trailing \r turned every value into
    one ending in a carriage return."""
    (tmp_path / "aiforge.env").write_text(
        "AIFORGE_ADMIN_URL=http://rig:8799\r\n")

    r = _run(tmp_path, [])

    assert "spoke of http://rig:8799" in r.stdout


def test_a_leftover_dot_env_is_noticed_but_not_read(tmp_path: Path):
    """Silently honouring one would be exactly the per-box drift this replaced."""
    (tmp_path / ".env").write_text("AIFORGE_ADMIN_URL=http://stale:8799\n")

    r = _run(tmp_path, [])

    assert ".env is ignored" in r.stderr
    assert "stale" not in r.stdout
