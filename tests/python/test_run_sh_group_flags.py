"""``run.sh --admin-url`` and ``--group``: naming the admin and its group.

Same shape as ``test_run_sh_admin_role``: the refusal branches run the real
script (they exit before any bootstrap), and the persistence half exercises the
shipped writer function itself rather than a reimplementation of it.

Both flags write to the env file, and both are the operator saying something the
machine cannot work out for itself — which box is the hub, and which pool this
one belongs to when the hub serves several.
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
    env["AIFORGE_IN_SANDBOX"] = "1"  # the app path; otherwise run.sh starts a container
    env.pop("AIFORGE_ADMIN_URL", None)
    env.pop("AIFORGE_ROLE", None)
    env.pop("AIFORGE_SYNC_GROUP", None)
    env.update(extra_env or {})
    return subprocess.run(["bash", str(dst), *args], cwd=str(tmp_path),
                          capture_output=True, text=True, env=env, timeout=60)


# ── the refusals ──────────────────────────────────────────────────────────

def test_admin_url_is_refused_on_a_box_holding_the_admin_role(tmp_path: Path):
    """The mirror of the rule --admin already enforces. A box that is both
    stamps ``derived: mesh`` while pushing to somebody else's hub."""
    proc = _run(tmp_path, ["--admin-url", "http://nuc:8799"],
                {"AIFORGE_ROLE": "admin"})

    assert proc.returncode == 2
    assert "cannot be both" in proc.stderr
    assert "--spoke" in proc.stderr          # …and says what to type instead


def test_admin_url_together_with_admin_is_refused(tmp_path: Path):
    proc = _run(tmp_path, ["--admin", "--admin-url", "http://nuc:8799"])

    assert proc.returncode == 2
    assert "cannot be both" in proc.stderr


def test_an_unusable_group_name_is_refused(tmp_path: Path):
    """The name becomes a directory component on the admin, so it takes the same
    alphabet ``sync.group.is_valid`` enforces — refused, never repaired."""
    proc = _run(tmp_path, ["--group", "../etc"])

    assert proc.returncode == 2
    assert "group name" in proc.stderr


def test_help_lists_both_new_flags(tmp_path: Path):
    proc = _run(tmp_path, ["--help"])

    assert proc.returncode == 0
    assert "--admin-url" in proc.stdout
    assert "--group" in proc.stdout


# ── both flags are per-run now ────────────────────────────────────────────
# They used to be written into the env file. The file is fixed and committed,
# so they apply to this process and print the variable to set for permanence.

def test_admin_url_applies_to_this_run_and_says_how_to_keep_it(tmp_path: Path):
    r = _run(tmp_path, ["--admin-url", "http://rig:8799", "--test"])

    assert "spoke of http://rig:8799 for THIS run" in r.stdout
    assert "AIFORGE_ADMIN_URL=http://rig:8799" in r.stdout
    assert not (tmp_path / "aiforge.env").exists(), "run.sh wrote a config file"


def test_group_applies_to_this_run_and_says_how_to_keep_it(tmp_path: Path):
    r = _run(tmp_path, ["--group", "eu-west", "--test"])

    assert "group eu-west for THIS run" in r.stdout
    assert "AIFORGE_SYNC_GROUP=eu-west" in r.stdout


def test_neither_flag_leaves_a_file_behind(tmp_path: Path):
    _run(tmp_path, ["--admin-url", "http://rig:8799", "--group", "eu", "--test"])

    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "aiforge.env").exists()
