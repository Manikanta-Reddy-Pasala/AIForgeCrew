# Sub #8 — Agent Delegation

**Date:** 2026-05-21
**Depends on:** none

## Goal

OH-parity `delegate_to_agent(role, prompt)` tool — Doer can spin up a sub-agent (Researcher, Planner, etc.) within its turn and receive that agent's output back.

## Module

`aiforge_core/runtime/tools/delegation.py`

## API

```python
def delegate_to_agent(role: str, prompt: str, timeout: int = 600) -> dict
```

Allowed `role` values come from `agents.yaml` (researcher / planner / refiner / triage / verifier).

Returns:

```python
{
  "ok": bool,
  "role": str,
  "output": str,        # delegated agent's response text
  "state_keys": list,   # session-state keys the delegate wrote
  "wall_s": float,
  "error": str?,
}
```

## Implementation

- Use ADK Runner with a single LlmAgent of the target role.
- Inherit base_url / model from agents.yaml.
- Hard wall cap (timeout).
- Soft-error on unknown role.
- `:Delegate` trace event with role + duration.

## Tests

- delegate to unknown role → soft error
- happy path: mock ADK runner returns output; assert output / state_keys returned
- timeout enforced via asyncio.wait_for
