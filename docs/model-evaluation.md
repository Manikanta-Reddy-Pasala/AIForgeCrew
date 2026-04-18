# Model Evaluation Matrix

> Phase P0 complete. P9 will add pass@1 / precision on full eval set.
> Runtime: LM Studio MLX on Mac Studio M3 Ultra 96 GB.
> Endpoint: `http://localhost:1234/v1` (OpenAI-compatible).

## Roles + criteria

| Role | Primary axis | Secondary |
|------|--------------|-----------|
| EM (cloud) | planning quality, ambiguity detection | cost/1k tokens |
| Tester | tool-use reliability (MCP + Playwright), test coverage breadth | speed |
| Sr Developer | code correctness at first pass | context window, speed |
| Sr Architect | review precision, security reasoning | hallucination rate |

## P0 Benchmark Results (2026-04-18)

Harness: `scripts/benchmark-models.sh`. Temperature 0, max_tokens 3000. Prompts: failing-test → code (Dev), browser-MCP planning (Tester), SQLi Flask review (Arch).

| Role | Model | Quant | Size | tok/s | Elapsed (s) | Reasoning tokens | Quality |
|------|-------|-------|-----:|------:|------------:|-----------------:|---------|
| Sr Developer | Qwen3.6-35B-A3B | MLX 4bit | 20.43 GB | **64.0** | 18.8 | 1142 | ✅ correct iterative `fibonacci` — all 3 tests pass |
| Tester | GLM-4.7-Flash | MLX 6bit | 24.36 GB | **54.7** | 31.7 | 1693 | ✅ 5 correct Playwright calls in order (navigate → fill × 2 → click → get_text) |
| Sr Architect | Gemma-4-31B-IT | MLX 4bit | 18.44 GB | **14.2** | 17.0 | 0 | ✅ caught SQLi (L10), debug-mode leak (L15), None-crash (L12), resource leak (L8-12), connection overhead (L8), missing input validation (L7) |

### Key observations

- **Dev (Qwen3.6)**: 95% of tokens consumed by internal thinking before emitting code. Final output is minimal and correct. MoE 3B active → fast.
- **Tester (GLM-4.7-Flash)**: 97% reasoning tokens. Once thinking completes, emits clean tool-call sequence. Validates τ²-Bench claim for Playwright MCP orchestration.
- **Architect (Gemma-4-31B)**: No reasoning mode (dense non-thinking). Direct review, dense-model precision evident. Lower tok/s (14.2) is expected for 31B dense; offset by lower call frequency (review-only).

### Throughput (concurrent)

Loading all 3 simultaneously resident is untested — P0 benchmark exercised them serially. KV cache + MoE arithmetic suggests concurrent Dev+Tester pair sustains ~40 tok/s each; Architect independent.

### Draft / speculative decoding

Draft models downloaded but speculative decoding not yet wired. To enable in LM Studio: per-model settings → "Draft model" dropdown.

| Target | Draft | Status |
|--------|-------|--------|
| Qwen3.6-35B-A3B | Qwen3-0.6B | downloaded, not wired |
| Gemma-4-31B-IT | Gemma-4-E2B-IT | downloaded, not wired |

### Watchlist (re-evaluate in P9)

- **Ternary Bonsai 8B** (PrismML, 2026-04-16) — 1.58-bit ternary weights, ~1.75 GB RAM, 75.5 avg benchmark. MLX port not yet on `mlx-community`. Revisit in 1-2 weeks. Potential use: triage agent, Mem0 summarizer, edge companion.

## Harness

- Script: `scripts/benchmark-models.sh` (uses curl `-w '%{time_total}'` for timing; awk for tok/s math; jq for content/reasoning_content fallback).
- Results log: `~/aiforge-logs/bench.txt` on Mac Studio.
- Re-run: `ssh manikanta@<ip> 'bash -s' < scripts/benchmark-models.sh`
