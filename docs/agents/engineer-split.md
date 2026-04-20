# Sr Developer split — reasoning vs coder (2026-04-20)

Basis: ONE-48 BOI parser v2 eval showed gemma-4-31b-it (dense 31B) reaches 11/13 in 20 tool calls where qwen3-coder-next (MoE 80B / 3B active) reaches 5/13 in 188 calls on the same reasoning-heavy ticket. Qwen is SWE-trained but MoE active-param count caps single-pass reasoning depth. Split rationale: match model shape to task shape.

## Agents

| Agent | ID | Model | Use for |
|-------|----|-------|---------|
| Sr Dev (Reasoning) | `28b8c064-bfcf-44e1-9e91-e37c39e0097c` | `gemma-4-31b-it` (dense 31B, 18GB) | schema design, metadata extraction, semantic column inference, state logic, format normalization, complex refactors |
| Sr Dev (Coder) | `e0502e94-0608-4fb9-9afa-b70d8dbf014a` | `qwen3-coder-next` (MoE 80B/3B, 42GB) | implementation from spec, rename/refactor, add endpoints, format conversion, test writing, boilerplate |

Both `role=engineer`, both `adapterType=hermes_local`, both report to Engineering Manager `35760e2f-4cef-4013-9aff-d93592b5f71e`.

## Labels

| Label | ID | Color | Meaning |
|-------|----|-------|---------|
| reasoning | `db58c603-5c1d-47f8-ae3b-59bb13486216` | purple `#8b5cf6` | route to Sr Dev (Reasoning) |
| code | `3d471283-6dd3-408a-9ae4-61465833d33b` | green `#10b981` | route to Sr Dev (Coder) |

## Routing

Manual: apply label + assign to matching agent in Paperclip UI.

Scripted: `bash scripts/route-ticket.sh <TICKET_ID> <reasoning|code>` — patches assignee + applies label.

## Orchestration caveat

Mac Studio has 64GB unified memory. Gemma 31B (18GB) + Qwen-Coder-Next (42GB) = 60GB total — near cap. Keep **one loaded at a time**. When dispatching:

1. Check which agent owns the next ticket.
2. If LM Studio has the other model loaded, unload + load the right one before waking the agent.
3. `scripts/boi-v2-direct.sh` shows the load/unload pattern.

A future Paperclip heartbeat pre-hook could automate this (check agent's `adapter_config.model`, ensure it's loaded in LM Studio, then wake). Not built yet.

## Eval-driven boundary (keep refining)

Prior data points:
- ONE-48 BOI v2 (reasoning-heavy): gemma-4-31b 11/13, qwen-coder 5/13, gemma-4-26b 1/13 (recursion self-DoS)
- Prior benchmarks (`docs/eval/sr-dev-bench.csv`): see per-row notes.

Add 2–3 validation tickets per category before treating the split as stable:
- **reasoning bucket**: another bank parser (different statement format), mongo schema migration, event-sourcing replay spec
- **code bucket**: rename symbol across N files, add CRUD endpoint with pre-written spec, Python 3.9 → 3.11 upgrade pass
