"""Text/grep assertions on run.sh — the 3-mode (hybrid default) structure.

Docker isn't available in CI, so these are static-text checks (no execution)
guarding the deploy-mode contract of run.sh:

  hybrid (default): infra-only docker + host api+runner
  --docker        : full stack in containers
  --lite          : all host, SQLite, no docker
"""
from pathlib import Path

import pytest

RUN_SH = Path(__file__).resolve().parents[2] / "run.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return RUN_SH.read_text()


def test_run_sh_exists(text: str) -> None:
    assert text.startswith("#!/usr/bin/env bash")


def test_default_mode_is_hybrid(text: str) -> None:
    assert "MODE=hybrid" in text
    # the default assignment (not just a flag branch) carries the comment
    assert "MODE=hybrid       # infra in docker, agent on host" in text


def test_flag_branches_exist(text: str) -> None:
    assert "--lite) MODE=lite ;;" in text
    assert "--docker) MODE=docker ;;" in text
    assert "--hybrid) MODE=hybrid ;;" in text


def test_dev_does_not_force_docker(text: str) -> None:
    # --dev is a host concept: if docker was selected, demote to hybrid
    assert "--dev) DEV=1; [[ $MODE == docker ]] && MODE=hybrid ;;" in text


def test_docker_infra_up_function(text: str) -> None:
    assert "_docker_infra_up() {" in text
    # sudo auto-fallback preserved
    assert "AIFORGE_DOCKER_SUDO" in text
    # docker-missing guidance points at --lite
    assert "./run.sh --lite" in text


def test_hybrid_brings_up_infra_only(text: str) -> None:
    # the hybrid branch starts exactly the infra services, NOT api/runner
    assert "_docker_infra_up postgres neo4j embed rerank" in text
    # ensure api/runner are never handed to compose `up` as explicit services
    assert "_docker_infra_up postgres neo4j embed rerank api" not in text
    assert "up -d --build api runner" not in text
    assert "up -d api runner" not in text


def test_hybrid_host_env_points_at_dockerized_infra(text: str) -> None:
    assert 'export AIFORGE_FORCE_PG=1' in text
    assert 'export AIFORGE_MEMORY_BACKEND=neo4j' in text
    assert 'export AIFORGE_EMBED_URL="http://127.0.0.1:8764"' in text
    assert 'export AIFORGE_RERANK_URL="http://127.0.0.1:8765"' in text
    assert '@127.0.0.1:${PG_PORT:-5432}/' in text
    assert 'bolt://127.0.0.1:7687' in text


def test_lockdown_env_exports(text: str) -> None:
    for key in (
        "AIFORGE_EXTERNAL_INGEST",
        "AIFORGE_DOCS_INDEX",
        "AIFORGE_ALLOW_WEB_FETCH",
        "AIFORGE_BROWSER_ALLOWLIST",
        "DO_NOT_TRACK",
        "HF_HUB_DISABLE_TELEMETRY",
        "LITELLM_TELEMETRY",
    ):
        assert f'export {key}="${{{key}:-' in text, key
    # external-ingest is forced OFF (code default is ON)
    assert 'export AIFORGE_EXTERNAL_INGEST="${AIFORGE_EXTERNAL_INGEST:-0}"' in text


def test_hybrid_host_runner_background_loop(text: str) -> None:
    assert "if [[ $MODE == hybrid ]]; then" in text
    assert "python -m aiforge_core.runtime.adk_runner" in text
    assert "RUNNER_PID=$!" in text
    assert "trap 'kill $RUNNER_PID 2>/dev/null' EXIT INT TERM" in text
    assert "AIFORGE_RUNNER_POLL_SEC:-10" in text


def test_test_flag_skips_docker(text: str) -> None:
    # --test only flips TEST; the whole docker case is gated on TEST=0
    assert "--test) TEST=1 ;;" in text
    assert "if [[ $TEST -eq 0 ]]; then" in text
    # the probe still runs (after venv setup)
    assert "aiforge_core.cli.connectivity_test" in text


def test_docker_mode_still_full_stack(text: str) -> None:
    # regression: --docker keeps the down-first full restart + exit
    assert "DOWN_FIRST=1" in text
    assert "down --remove-orphans" in text
    assert 'echo "  AIForge (docker) → http://localhost:${PORT}/ui/"' in text
