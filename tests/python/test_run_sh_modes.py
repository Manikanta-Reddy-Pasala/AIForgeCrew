"""Behavioural tests for run.sh's argument parsing and early-exit branches.

The stack moved to a single SQLite mode: `MODE=hybrid`, the `--lite`/`--hybrid`
flag branches, `_INFRA_SVCS`, and the docker-infra-only bring-up all stopped
being real concepts in run.sh a while back (`--docker`/`--lite`/`--hybrid`/
`--no-build` are now backwards-compat no-ops -- see the "Flags:" comment block
in run.sh). The previous version of this file grepped run.sh's source text for
those exact strings, so it rotted the moment the script was refactored, with
no actual behaviour changing.

These tests instead genuinely run `bash run.sh ...` (with a scrubbed/overridden
environment) and assert on observable effects -- exit code, stdout/stderr,
whether the server-launch banner was reached -- rather than on implementation
strings. The one branch that's genuinely impractical to exercise from a bare
checkout (the full uv/venv bootstrap that unconditionally runs before `--test`
can be reached) is worked around by reusing this repo's already-installed
`.venv` rather than by falling back to grepping source; see the comment on
that test for the full reasoning.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RUN_SH = Path(__file__).resolve().parents[2] / "run.sh"

# Printed only once run.sh has walked all the way through the venv bootstrap,
# web build, etc. and is about to launch the runner + uvicorn. Its absence is
# how we prove a given invocation took an *early*-exit branch rather than
# falling through to actually starting the stack.
_LAUNCH_BANNER_SENTINEL = "code context: RepoMap + CodeGraph"


def _bash(script: Path, args: list[str], cwd: Path, extra_env: dict | None,
          timeout: float) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=str(cwd), capture_output=True, text=True, env=env, timeout=timeout,
    )


def _run(args: list[str], extra_env: dict | None = None,
         timeout: float = 30) -> subprocess.CompletedProcess:
    """Run the real run.sh in place (repo root as cwd)."""
    return _bash(RUN_SH, args, RUN_SH.parent, extra_env, timeout)


def _run_isolated(tmp_path: Path, args: list[str], extra_env: dict | None = None,
                   timeout: float = 30) -> subprocess.CompletedProcess:
    """Copy run.sh into an empty tmp_path (deliberately no .venv) and run it
    from there, so the "no .venv yet" maintenance guard can be exercised
    honestly, without needing (or polluting) the real toolchain."""
    dst = tmp_path / "run.sh"
    shutil.copy(RUN_SH, dst)
    env = {"AIFORGE_CONFIG_DIR": str(tmp_path / "cfg")}
    env.update(extra_env or {})
    return _bash(dst, args, tmp_path, env, timeout)


def test_run_sh_parses_as_valid_bash() -> None:
    proc = subprocess.run(["bash", "-n", str(RUN_SH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_help_exits_zero_and_lists_the_real_flags() -> None:
    proc = _run(["--help"])
    assert proc.returncode == 0, proc.stderr
    for flag in (
        "--port", "--host", "--dev", "--admin", "--skip-web", "--test",
        "--reset-config", "--with-langfuse", "--install-model2vec", "--migrate",
    ):
        assert flag in proc.stdout, flag
    # sanity: --help took the early-exit path and never reached the launch banner
    assert _LAUNCH_BANNER_SENTINEL not in proc.stdout


def test_unknown_flag_exits_nonzero_with_message() -> None:
    proc = _run(["--this-flag-does-not-exist"])
    assert proc.returncode != 0
    assert "unknown arg: --this-flag-does-not-exist" in proc.stderr


def test_port_and_host_values_are_consumed_not_left_dangling() -> None:
    # If --port/--host failed to `shift` their value, the value itself (e.g.
    # "9999") would be re-parsed as the *next* flag and reported as the
    # unknown arg instead of the sentinel that actually follows it.
    proc = _run(["--port", "9999", "--host", "0.0.0.0", "--sentinel-xyz"])
    assert proc.returncode != 0
    assert "unknown arg: --sentinel-xyz" in proc.stderr
    assert "9999" not in proc.stderr
    assert "0.0.0.0" not in proc.stderr

    # order shouldn't matter either
    proc2 = _run(["--host", "0.0.0.0", "--port", "9999", "--sentinel-xyz"])
    assert proc2.returncode != 0
    assert "unknown arg: --sentinel-xyz" in proc2.stderr


def test_legacy_noop_flags_are_still_accepted() -> None:
    """--lite/--hybrid/--no-build are kept as backwards-compat no-ops (the
    stack is single-mode SQLite now). Pin that they don't error and don't
    swallow an extra token -- a real compatibility guarantee worth locking in."""
    proc = _run(["--lite", "--hybrid", "--no-build", "--sentinel-xyz"])
    assert proc.returncode != 0
    assert "unknown arg: --sentinel-xyz" in proc.stderr


def test_every_flag_shifts_exactly_its_own_tokens() -> None:
    """One combined pass over every non-maintenance flag (value-taking and
    bare) with a sentinel unknown flag at the very end. If any branch failed
    to `shift` correctly -- shifted too much, too little, or not at all -- the
    sentinel would either never be reached, or a stray value would be
    misreported as the unknown arg instead of it."""
    proc = _run([
        "--port", "9999", "--host", "0.0.0.0",
        "--dev", "--admin", "--skip-web", "--with-graphify", "--with-langfuse",
        "--install-model2vec", "--migrate", "--reset-config",
        "--lite", "--hybrid", "--no-build",
        "--sentinel-final",
    ])
    assert proc.returncode != 0
    assert "unknown arg: --sentinel-final" in proc.stderr


@pytest.mark.parametrize("flag", ["--dedupe", "--recompact-all", "--migrate-okf", "--purge-code"])
def test_maintenance_flags_exit_before_touching_the_stack(tmp_path: Path, flag: str) -> None:
    """Maintenance commands need an existing .venv (they run a migrations
    module inside it) and must refuse cleanly -- not fall through to the
    uv/venv bootstrap or the server launch -- when there isn't one yet. Run
    from an isolated copy with no .venv so this is a real, not stubbed, check
    of that guard."""
    proc = _run_isolated(tmp_path, [flag])
    assert proc.returncode == 1
    assert "no .venv yet" in proc.stderr
    assert _LAUNCH_BANNER_SENTINEL not in proc.stdout


def test_test_flag_reaches_the_probe_not_the_launch_banner(tmp_path: Path) -> None:
    """--test must take the connectivity-probe early exit, not boot the
    server.

    Reaching the --test branch requires the FULL python-env bootstrap (uv venv
    create + `pip install -e .`) to run first -- that step is unconditional in
    run.sh, so exercising it from a bare tmp_path (no .venv) would mean a real,
    slow, network-dependent dependency install on every test run. That is the
    "genuinely impractical to execute honestly" case: instead of grepping
    source for it, we run the *real* run.sh in place, reusing this repo's
    already-installed .venv (so the bootstrap is a fast no-op), and isolate
    state via AIFORGE_CONFIG_DIR plus the SKIP_* toggles so it can't write into
    the real ~/.aiforge or attempt instructor/codegraph installs.

    The probe itself does one real HTTP GET to the configured model endpoint --
    this assertion tolerates either OK or FAIL (no LLM needs to be reachable
    for the test to be valid; connection-refused resolves near-instantly, and
    the default probe timeout is only used as an upper bound).
    """
    proc = _run(["--test"], extra_env={
        "AIFORGE_CONFIG_DIR": str(tmp_path / "cfg"),
        "AIFORGE_SKIP_AIDER": "1",
        "AIFORGE_SKIP_INTEGRATIONS": "1",
        "AIFORGE_SKIP_CODEGRAPH": "1",
        "AIFORGE_AUTO_MIGRATE": "0",
        "AIFORGE_MIGRATE_OKF": "0",
        "AIFORGE_FIX_PERMS": "0",
    }, timeout=90)
    assert proc.returncode in (0, 1), proc.stderr
    assert "AIForge connectivity test" in proc.stdout
    assert _LAUNCH_BANNER_SENTINEL not in proc.stdout
