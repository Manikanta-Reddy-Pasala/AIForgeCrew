# Sub #9 — Unified Budget Tracker

**Date:** 2026-05-21
**Depends on:** none

## Goal

Single accounting layer for per-call cost + tokens across EscalatingLlm, loop_budget, ADK. OH-parity LLMMetrics.

## Module

`aiforge_core/runtime/budget.py`

## API

```python
@dataclass
class Spend:
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ts: float

class BudgetTracker:
    def record(role, model, input_tokens, output_tokens, cost_usd) -> Spend
    def total() -> dict             # {input_tokens, output_tokens, cost_usd, calls}
    def by_role() -> dict[role, dict]
    def by_model() -> dict[model, dict]
    def reset() -> None
    def to_json() -> str

# Module-level singleton
tracker: BudgetTracker
```

## Implementation

- Single in-memory ring (cap 1000 entries by default; oldest evicted).
- Atomic-ish via threading.Lock.
- `:Cost` trace event per record() call.
- JSON serializer for end-of-ticket audit.

## Tests

- record + total
- by_role / by_model aggregation
- ring eviction at cap
- reset
- thread safety (concurrent records → no lost updates)
- to_json round-trip
