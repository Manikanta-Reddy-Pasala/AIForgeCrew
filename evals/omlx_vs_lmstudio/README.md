# oMLX vs LM Studio Benchmark

Comparison eval — does [jundot/omlx](https://github.com/jundot/omlx) beat LM Studio on the Mac Studio for the two models AIForge actually runs in prod?

## Hypothesis

oMLX advertises two architectural wins over LM Studio:

1. **Continuous batching** — multiple concurrent requests share decode steps instead of queueing serially.
2. **Tiered KV cache** — hot RAM + cold SSD persistence with prefix reuse across turns/conversations.

Both servers wrap `mlx-lm` for raw decode, so **single-stream cold throughput should be roughly tied**. Real wins (if any) should show up in:

- multi-turn conversations sharing a system/document prefix → omlx hits cache, LM Studio re-prefills
- N≥2 concurrent requests → omlx batches, LM Studio serializes

If omlx loses single-stream by ≥10% or fails to win the multi-turn/concurrent cases, the switch is not worth the operational cost (yet-another daemon, MLX-only models, no GGUF fallback).

## Models Under Test

| Slot | Display name | HF / MLX repo | Use in AIForge |
|------|--------------|---------------|----------------|
| coder | `qwen3-coder-30b-a3b-mlx` | `mlx-community/Qwen3-Coder-30B-A3B-Instruct-MLX` | Developer (v5/v6 pipeline) |
| general | `granite-4.1-h-30b-mlx` | `ibm-granite/granite-4.1-h-30b-MLX` | job-apply-local + general agent |

Both already MLX-cached on the Mac Studio (see `~/.cache/lm-studio/models` or `~/.lmstudio/models`).

## Test Matrix

Each `(server, model)` cell runs three phases. Same prompt set, same seeds, fixed `max_tokens`, `temperature=0.2`.

| Phase | Description | What it measures | Expect |
|-------|-------------|------------------|--------|
| **A** Cold single | 1 request, fresh process, ~1.5K-token prompt → 512 out | TTFT, prompt-eval tok/s, decode tok/s | tie (±5%) |
| **B** Warm multi-turn | 4 turns sharing 1.5K system+context prefix, each +200 tok user → 256 out | TTFT turn 2..4 | omlx wins if KV reuse works |
| **C** Concurrent | 4 parallel single-shot requests, 512 in / 256 out each | aggregate tok/s, p50/p95 wall latency | omlx wins via batching |

8 cells total (2 servers × 2 models × A/B/C → actually 12, but C reuses A prompts).

## Servers

| | LM Studio | oMLX |
|---|---|---|
| OpenAI-compat endpoint | `http://localhost:1234/v1` | `http://localhost:8000/v1` |
| Model loading | GUI or `lms load --ttl 43200` (see memory: TTL gotcha) | `omlx serve --model-dir ~/models` or auto-load on first req |
| Notes | Reuses existing MLX models in `~/.lmstudio/models` | MLX-only; point `--model-dir` at the same store if possible |

**Important:** only one server keeps the GPU/memory hot at a time. Bench script stops the other before each phase to avoid contention. We do **not** want to test "what happens when both compete for memory."

## How To Run

Three steps, all driven from this Mac.

```bash
# 1. Install omlx CLI on the Mac Studio (Homebrew, idempotent)
./run-remote.sh install

# 2. Smoke-test both servers (loads each model under each server, single ping)
./run-remote.sh smoke

# 3. Full bench, both servers, both models, all phases. ~25 min wall.
./run-remote.sh bench
```

Results land in `results/<UTC-timestamp>/{server}_{model}_{phase}.json` plus a summary `results/<ts>/summary.md` with side-by-side tables.

### Per-phase fairness rules

1. **Warm-up:** discard first request after a model load (compile + first-token warm-up).
2. **Sample size:** N=5 per cell for A; N=3 conversations × 4 turns for B; N=3 batches × 4 reqs for C.
3. **No GUI:** quit the LM Studio app, run `lms server start` headless. Same for omlx (`brew services start omlx`, not the menu-bar app).
4. **Cooldown:** 10s sleep + GPU memory check between cells.
5. **TTFT:** measured from first byte of first non-empty `choices[0].delta.content` over SSE stream.

## Result Interpretation

Decision rule (post-hoc):

- omlx wins by ≥15% on phase B (multi-turn) **and** ≥20% on phase C (concurrent) → **adopt** for AIForge dev/general agents
- omlx wins B or C only → adopt only for the workload that wins (likely concurrent → dev pipeline)
- omlx loses A by >10% → reject regardless; raw throughput tax not worth it
- tie everywhere → stay on LM Studio (lower operational surface, GUI debugging)

## Layout

```
evals/omlx_vs_lmstudio/
├── README.md           ← this file
├── bench.py            ← async harness (httpx + asyncio)
├── prompts.py          ← fixed test prompts (coder + general)
├── install-omlx.sh     ← runs on Mac Studio, brew install
├── run-remote.sh       ← runs here, ssh-drives MS
└── results/            ← gitignored; output land here per run
```
