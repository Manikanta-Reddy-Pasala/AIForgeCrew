# Sub #3 — Persistent IPython Kernel

**Date:** 2026-05-21
**Depends on:** Sub #1 (session lifecycle pattern)

## Goal

OH-parity persistent IPython kernel per ADK run. Tool: `execute_ipython_cell(code, timeout=60)`.

## Module

`aiforge_core/runtime/tools/ipython_kernel.py`.

## Lifecycle

- One `IPythonKernelManager` per ADK run_id, lazily started on first call.
- Variables, imports, defs persist across calls within the run.
- Destroyed in `_run_pipeline` finally block alongside bash/browser.

## Returns

```python
{
  "ok": bool,
  "stdout": str,        # capped 8 KB
  "stderr": str,        # capped 8 KB
  "result": str,        # last expression repr if any
  "truncated": bool,
  "error": "timeout"|"kernel_missing"|...
}
```

## Implementation

- Use `jupyter_client.manager.KernelManager` (ships with ipykernel).
- Optional dep: if `jupyter_client` not installed → `{ok: False, error: "kernel_missing"}`.
- Hard timeout 60s default.
- `:Cell` trace event per call.

## Tests

- happy: assignment in cell 1, read variable in cell 2 → state persists
- syntax error → ok=False, stderr populated
- timeout
- missing jupyter_client → soft error
