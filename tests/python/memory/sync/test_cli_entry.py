"""The CLI is reachable the way an operator actually invokes it.

Every other test in this package calls ``run_once``/``sync_with`` directly, so
none of them notice if the module cannot be executed at all. A missing
``if __name__ == "__main__"`` guard makes ``python -m ...loop`` import the
module and exit silently — no output, no error, exit 0 — which reads exactly
like "ran fine, nothing to sync". Found on a live two-machine run, not here.
"""
from __future__ import annotations

import subprocess
import sys


def _run(args: list[str], tmp_path, extra_env: dict | None = None):
    env = {
        "PATH": "/usr/bin:/bin",
        "AIFORGE_CONFIG_DIR": str(tmp_path / "cfg"),
        "AIFORGE_MEMORY_MD_DIR": str(tmp_path / "md"),
        "AIFORGE_MEMORY_DB_PATH": str(tmp_path / "memory.db"),
        "AIFORGE_MEMORY_BACKEND": "sqlite",
        "AIFORGE_PEER_ID": "book",
        "AIFORGE_AUTODETECT_CTX": "0",
    }
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "aiforge_core.memory.sync.loop", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_module_is_executable_with_dash_m(tmp_path):
    """`python -m ...loop --once` must actually run a cycle, not no-op."""
    proc = _run(["--once"], tmp_path)

    assert proc.returncode == 0, proc.stderr
    # No peers are configured, so a cycle is a no-op *result* — but the process
    # must have got as far as main(). argparse proves it: --help only works if
    # the guard fired.
    help_proc = _run(["--help"], tmp_path)
    assert help_proc.returncode == 0
    assert "--once" in help_proc.stdout
    assert "--interval" in help_proc.stdout


def test_an_unknown_flag_is_rejected(tmp_path):
    """Confirms argparse is genuinely reached, rather than the module no-opping."""
    proc = _run(["--nonsense"], tmp_path)

    assert proc.returncode != 0
    assert "unrecognized arguments" in proc.stderr
