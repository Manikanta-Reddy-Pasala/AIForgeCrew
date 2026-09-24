"""LM Studio helper scripts take the model from env — never a hard-coded id.

A baked-in model id made the box load a second large model next to (or
instead of) the one the operator configured. With no model set, each script
must say so and skip the load rather than guess one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = (
    ROOT / "scripts/runtime/install.sh",
    ROOT / "scripts/runtime/nuc/lms-ensure.sh",
    ROOT / "packages/aiforge_memory/aiforge_memory/ops/run_all_summaries.sh",
)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_model_id_is_hard_coded(script):
    assert "qwen" not in script.read_text().lower()


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_parses(script):
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_lms_ensure_skips_when_no_model_is_configured(tmp_path):
    """Host set, no model → clean skip before any ssh (ssh would fail here)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AIFORGE_LMS_")}
    env.update(AIFORGE_LMS_HOST="nobody@invalid.invalid",
               AIFORGE_CONFIG_DIR=str(tmp_path),
               PATH=str(tmp_path) + os.pathsep + env.get("PATH", ""))
    # A fake ssh that records any call — the skip must happen before it.
    fake = tmp_path / "ssh"
    fake.write_text(f"#!/bin/sh\ntouch {tmp_path}/ssh-called\nexit 1\n")
    fake.chmod(0o755)

    out = subprocess.run(["bash", str(SCRIPTS[1])], env=env,
                         capture_output=True, text=True, timeout=30)

    assert out.returncode == 0, out.stderr
    assert "no model configured" in out.stdout
    assert not (tmp_path / "ssh-called").exists()
