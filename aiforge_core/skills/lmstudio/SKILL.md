---
name: aiforge-lmstudio
description: LM Studio model lifecycle — ensure a model is loaded at a target context size, list loaded models, verify inference. Use BEFORE dispatching hermes runs when the target model may not be resident or may have drifted to a smaller ctx window under RAM pressure.
version: 1.0.0
platforms: [macos]
---

# aiforge-lmstudio

## List currently loaded models + their actual context size

```bash
~/.lmstudio/bin/lms ps
```

## Ensure a model is loaded at a target ctx (unloads others, verifies post-load)

Canonical recovery for "Hermes exits after 2-15s with EXIT=1" — usually caused
by LM Studio silently loading below target ctx under RAM guardrail.

```bash
MODEL="${MODEL:-qwen3-coder-next}"
CTX="${CTX:-65536}"
bash ~/AIForgeCrew/scripts/lib/ensure-model.sh "$MODEL" "$CTX"
```

Script behavior:
1. `lms unload --all`
2. `lms load $MODEL -c $CTX`
3. Verifies `lms ps` reports actual ctx ≥ target
4. Syncs `~/.hermes/context_length_cache.yaml` to observed ctx
5. Smoke test via `curl /v1/chat/completions`

## Available AIForgeCrew role models

| Model | Role | Target ctx |
|---|---|---|
| gemma-4-31b-it | Sr Developer (planning + review) | 65536 |
| qwen3-coder-next | Developer (code + tests) | 65536 or 131072 |

## Probe REST API

```bash
curl -s http://localhost:1234/v1/models | python3 -m json.tool | head -30
```

## When to use

- Before any `scripts/srdev-run.sh` / `dev-run.sh` / `review-run.sh` — ensures correct model loaded at correct ctx.
- After LM Studio restart — `lms ps` may look empty while REST API still serves models; `ensure-model.sh` will reload cleanly.
- After OOM-style symptoms (EXIT=1 after 2-15s) — the ctx silently dropped.

## Known quirk — `:N` JIT clones

LM Studio creates `qwen3-coder-next:2` style clones when a second load request
arrives while one is running. `ensure-model.sh` resolves this by unloading all
first.
