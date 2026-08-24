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
import stat
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


# ── the writer, as shipped ────────────────────────────────────────────────

def _write_role_harness(env_file: Path, want: str) -> subprocess.CompletedProcess:
    """Run run.sh's own ``_write_role`` against ``env_file``.

    The function is lifted out of the script rather than reimplemented, so this
    tests the shipped code: a copy would drift, and every bug below (a dropped
    file mode, a stray .tmp) was in the details of these four lines.
    """
    src = RUN_SH.read_text(encoding="utf-8")
    body = re.search(r"^_write_role\(\) \{.*?^\}$", src, re.S | re.M)
    assert body, "run.sh no longer defines _write_role"
    script = (f'set -euo pipefail\nENV_FILE="{env_file.name}"\n'
              f'_env_role_file="${{ENV_FILE}}"\n{body.group(0)}\n'
              f'_write_role "{want}"\n')
    return subprocess.run(["bash", "-c", script], cwd=str(env_file.parent),
                          capture_output=True, text=True, timeout=30)


def test_the_role_replaces_any_prior_role_line(tmp_path: Path):
    """``grep -v`` exits 1 when it selects nothing, so a file whose ONLY content
    was a role line used to keep the old line and gain the new one — two
    contradictory roles, which is exactly what the rewrite exists to prevent."""
    env = tmp_path / ".env"
    env.write_text("AIFORGE_ROLE=spoke\n", encoding="utf-8")

    proc = _write_role_harness(env, "admin")

    assert proc.returncode == 0, proc.stderr
    assert env.read_text(encoding="utf-8") == "AIFORGE_ROLE=admin\n"
    assert not (tmp_path / ".env.tmp").exists(), "a stray tmp file was left behind"


def test_other_settings_survive_the_rewrite(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("AIFORGE_LM_API_KEY=sk-secret\nAIFORGE_ROLE=spoke\n"
                   "AIFORGE_ALLOW_SSH=1\n", encoding="utf-8")

    _write_role_harness(env, "admin")

    text = env.read_text(encoding="utf-8")
    assert "AIFORGE_LM_API_KEY=sk-secret" in text
    assert "AIFORGE_ALLOW_SSH=1" in text
    assert text.count("AIFORGE_ROLE=") == 1
    assert "AIFORGE_ROLE=admin" in text


def test_the_file_mode_is_preserved(tmp_path: Path):
    """.env is where .env.example tells operators to put their API keys. A naive
    ``> tmp && mv`` creates the replacement under the umask, so a 0600 secrets
    file came back 0644 — world-readable — after one ``./run.sh --admin``."""
    env = tmp_path / ".env"
    env.write_text("AIFORGE_LM_API_KEY=sk-secret\n", encoding="utf-8")
    env.chmod(0o600)

    _write_role_harness(env, "admin")

    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_an_empty_want_only_removes_the_line(tmp_path: Path):
    """``--spoke``: the way back out of a persisted role, and therefore the way
    the admin is moved to another machine."""
    env = tmp_path / ".env"
    env.write_text("AIFORGE_ROLE=admin\nAIFORGE_ALLOW_SSH=1\n", encoding="utf-8")

    proc = _write_role_harness(env, "")

    assert proc.returncode == 0, proc.stderr
    assert env.read_text(encoding="utf-8") == "AIFORGE_ALLOW_SSH=1\n"


def test_a_missing_env_file_is_created(tmp_path: Path):
    env = tmp_path / ".env"

    proc = _write_role_harness(env, "admin")

    assert proc.returncode == 0, proc.stderr
    assert env.read_text(encoding="utf-8") == "AIFORGE_ROLE=admin\n"
