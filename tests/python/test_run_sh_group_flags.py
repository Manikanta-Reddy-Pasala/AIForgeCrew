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


# ── the writer, as shipped ────────────────────────────────────────────────

def _write_env_harness(env_file: Path, key: str, want: str):
    """Run run.sh's own ``_write_env_line`` against ``env_file``.

    Lifted out of the script rather than reimplemented, so this tests the
    shipped code — a copy would drift, and every bug the role tests document (a
    dropped file mode, a stray .tmp, a grep exit code) lives in these lines.
    """
    src = RUN_SH.read_text(encoding="utf-8")
    body = re.search(r"^_write_env_line\(\) \{.*?^\}$", src, re.S | re.M)
    assert body, "run.sh no longer defines _write_env_line"
    script = (f'set -euo pipefail\nENV_FILE="{env_file.name}"\n'
              f'_env_role_file="${{ENV_FILE}}"\n{body.group(0)}\n'
              f'_write_env_line "{key}" "{want}"\n')
    return subprocess.run(["bash", "-c", script], cwd=str(env_file.parent),
                          capture_output=True, text=True, timeout=30)


def test_the_admin_url_replaces_any_prior_line(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("AIFORGE_ADMIN_URL=http://old:8799\n", encoding="utf-8")

    proc = _write_env_harness(env, "AIFORGE_ADMIN_URL", "http://nuc:8799")

    assert proc.returncode == 0, proc.stderr
    assert env.read_text(encoding="utf-8") == "AIFORGE_ADMIN_URL=http://nuc:8799\n"
    assert not (tmp_path / ".env.tmp").exists(), "a stray tmp file was left behind"


def test_the_group_line_does_not_disturb_the_role_line(tmp_path: Path):
    """The three persisted settings share one writer, and each must rewrite
    only its own key — a shared regex that matched a prefix would silently
    demote the admin while recording a group."""
    env = tmp_path / ".env"
    env.write_text("AIFORGE_ROLE=admin\nAIFORGE_ADMIN_URL=http://nuc:8799\n",
                   encoding="utf-8")

    _write_env_harness(env, "AIFORGE_SYNC_GROUP", "cellular")

    text = env.read_text(encoding="utf-8")
    assert "AIFORGE_ROLE=admin" in text
    assert "AIFORGE_ADMIN_URL=http://nuc:8799" in text
    assert "AIFORGE_SYNC_GROUP=cellular" in text


def test_other_settings_survive_the_rewrite(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("AIFORGE_LM_API_KEY=sk-secret\nAIFORGE_SYNC_GROUP=old\n"
                   "AIFORGE_ALLOW_SSH=1\n", encoding="utf-8")

    _write_env_harness(env, "AIFORGE_SYNC_GROUP", "cellular")

    text = env.read_text(encoding="utf-8")
    assert "AIFORGE_LM_API_KEY=sk-secret" in text
    assert "AIFORGE_ALLOW_SSH=1" in text
    assert text.count("AIFORGE_SYNC_GROUP=") == 1
    assert "AIFORGE_SYNC_GROUP=cellular" in text


def test_the_file_mode_is_preserved(tmp_path: Path):
    """.env is where .env.example tells operators to put their API keys."""
    env = tmp_path / ".env"
    env.write_text("AIFORGE_LM_API_KEY=sk-secret\n", encoding="utf-8")
    env.chmod(0o600)

    _write_env_harness(env, "AIFORGE_SYNC_GROUP", "cellular")

    assert stat.S_IMODE(env.stat().st_mode) == 0o600
