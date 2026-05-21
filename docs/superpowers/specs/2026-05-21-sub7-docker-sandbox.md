# Sub #7 — Docker Sandbox Runtime

**Date:** 2026-05-21
**Depends on:** Sub #1 (bash session lifecycle pattern)

## Goal

OH-parity Docker-isolated exec for `bash` and `execute_ipython_cell`. Opt-in per agent via `runtime: adk_agent_with_ga_docker` in agents.yaml. Falls back to host exec when Docker daemon absent.

## Module

`aiforge_core/runtime/docker_sandbox.py`

## API

```python
def is_enabled() -> bool                 # AIFORGE_DOCKER_SANDBOX env + docker bin present
def ensure_container(run_id: str) -> str # returns container id; lazy create
def exec_in_container(run_id: str, command: str, timeout: int) -> dict
def destroy_container(run_id: str) -> None
```

Default image: `python:3.12-slim` with build tools added. Override via `AIFORGE_DOCKER_IMAGE`.

## Implementation

- Use Docker CLI (`docker run -d` + `docker exec`) — no Python SDK dep.
- Container name: `aiforge-sandbox-{run_id}`.
- Workspace mounted RO to `/workspace`; writes go to per-run tmpfs `/work`.
- Network restricted via `--network=bridge` (default) — production overrides to `--network=none`.
- 1 GB memory + 2 CPU cap.
- Destroy on ADK finish callback.

## Tests

- is_enabled: env unset → False; env set + docker missing → False; both → True (mocked)
- exec mock-tests: returncode 0 / nonzero / timeout
- container reuse across calls (same run_id)
- destroy idempotent

## Wiring

`tools/bash.py` consults `docker_sandbox.is_enabled()` per call; when true, delegates to `exec_in_container` instead of tmux. Same return shape so model sees no difference.
