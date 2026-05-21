# Sub #4 — Memory Condenser

**Date:** 2026-05-21
**Depends on:** none (orthogonal to tools/)

## Goal

Cap ADK session.events context growth on long tickets. Three OH-parity strategies, agent-selectable via `agents.yaml`.

## Module

`aiforge_core/runtime/condensers.py`

## Strategies

| name | behavior |
|---|---|
| `noop` | identity — keep all events |
| `recent` | keep last N events verbatim; drop older |
| `amortized` | once events > threshold, compress oldest half into one `<condensed>` summary block (heuristic: keep tool result summaries, drop raw text) |
| `llm` | (deferred — needs LLM call; stub returns recent fallback) |

## API

```python
def condense(events: list[dict], strategy: str, **kwargs) -> list[dict]
```

Returns reduced event list; pure function.

## Integration

ADK ContextFilterPlugin already keeps last N invocations. Condenser is a NEW filter that runs ALONGSIDE for per-event compression. Wire as `BeforeAgentCallback` in pipeline.

## Tests

- noop: identity
- recent: 100 events → keep 20
- amortized: 100 events → < 60 events after, contains a `<condensed>` marker
- llm: stub returns recent fallback when LLM unset
