# LM Studio KV-Cache Quantization — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan
**Author:** pairing session

## Problem

Local serving runs **LM Studio** (`lms load … --context-length …`,
see `aiforge_core/runtime/local_starter.py`), not raw `mlx_lm.server`
(README is stale). KV cache memory scales linearly with context length ×
parallel lanes. ONE-117 showed the failure mode: a 131K-ctx KV cache ×
4 parallel lanes blew past Mac Studio's 96 GB unified memory and
triggered MLX Metal command-buffer aborts. The current mitigation is
`PARALLEL=1` + a hard ctx floor — but the fp16 KV cache still caps how
much context fits.

4-bit KV-cache quantization cuts KV memory ~4× with effectively lossless
output (per the reference article's measurements and our own prior 8-bit
enablement on `nex`, obs-28577). That headroom lets larger contexts fit
within 96 GB and further relieves the OOM pressure.

The reference article's package (**VeloxQuant-MLX / `mlx_kv_quant`**) is
**not viable for us**: it injects a `KVCacheBuilder` cache object into
in-process `mlx_lm.generate(cache=…)`. We serve over HTTP through LM
Studio's closed server, which exposes no custom-cache hook. The
supported lever is LM Studio's own KV-cache quantization, applied at
model load.

## Approach (chosen)

Wire LM Studio KV-cache quantization into the standard model-load path,
gated by env + a per-model manifest. Stays on LM Studio (preserves the
ONE-116/117 context-length / parallel / TTL behaviour). No new
dependency. Builds on the existing obs-28583 KV-quant automation rather
than replacing serving.

Rejected:
- *Switch to raw `mlx_lm.server --kv-bits`* — gives native CLI control
  but drops LM Studio's model management / TTL / warmup that the runner
  depends on; larger blast radius.
- *VeloxQuant custom server wrapper* — reimplements serving around a
  fragile third-party package; highest cost, lowest reliability.

## Critical safety constraint

**MLX vision models are incompatible with KV-cache quantization**
(obs-28581 / 28582: adding KV-quant config broke `nex-n2-mini` vision
model load). Therefore KV-quant is **opt-in per text model**;
vision and embedding models are **excluded** in the manifest. The load
path must never apply KV-quant to a model flagged non-text.

## Architecture

### Configuration surface

- New env `AIFORGE_LMS_KV_BITS` — default `4` for text models, `0`
  (off) disables globally. Mirrors the existing `AIFORGE_LMS_*` knob
  convention in `local_starter.py` (`_int_env`).
- **Per-model manifest** in `scripts/load-models.sh` (or a sidecar
  `models.manifest.json`): each entry records `kv_bits` and a
  `kind: text|vision|embedding` flag. `kind != text` ⇒ KV-quant skipped
  regardless of the env default. Reproducibility rule: manifest-driven,
  no ad-hoc SSH provisioning.

### Load path change

`local_starter.py` `load_cmd` builder gains the KV-quant argument when:
`AIFORGE_LMS_KV_BITS > 0` **and** the model is text-kind. Exact `lms`
flag / config-key is **confirmed against the installed LM Studio version
during implementation** (the KV-quant setting is currently applied via
LM Studio's per-model load config, not a documented generic CLI flag —
the obs-28583 automation script is the reference for the precise
mechanism). The builder must degrade gracefully if the running `lms`
version doesn't accept the flag (log + load without KV-quant rather than
fail the load).

### Reproducibility

The obs-28583 ops script (currently external, on Mac Studio) is brought
into the repo as the canonical KV-quant config step, invoked from the
load path / `scripts/load-models.sh`, so a fresh provision reproduces the
KV-quant state from the manifest with no manual GUI step.

## Data flow

`AIFORGE_LMS_KV_BITS` + manifest `kind`/`kv_bits` → `local_starter`
load command → LM Studio loads model with quantized KV cache → HTTP
serving unchanged → callers (LiteLLM/EscalatingLlm) see no API
difference, only larger usable context / lower RAM.

## Components touched

| File | Change |
|------|--------|
| `aiforge_core/runtime/local_starter.py` | KV-quant arg in `load_cmd`, gated by `AIFORGE_LMS_KV_BITS` + text-kind check; graceful degrade on unsupported `lms` |
| `scripts/load-models.sh` (+ manifest) | per-model `kv_bits` + `kind` manifest; canonical KV-quant config step (from obs-28583) |
| env docs / README | document `AIFORGE_LMS_KV_BITS`; correct stale `mlx_lm.server` note to LM Studio |
| `tests/` | manifest parse: vision/embedding ⇒ no KV-quant; env=0 ⇒ omitted; text+env=4 ⇒ arg present; load_cmd builder unit |

## Validation

- Re-run the 85K-ctx ceiling probe (obs-28564) per **text** model with
  `kv_bits=4`: confirm output quality unchanged vs fp16 baseline and
  measured RAM drop.
- Confirm a vision model (nex-n2-mini) still loads (KV-quant skipped) —
  regression guard for obs-28582.

## Out of scope

- 3-bit / 2-bit KV-quant (4-bit only; article notes 2-bit breaks
  generation, 3-bit marginal).
- VeloxQuant package adoption.
- Migrating local serving off LM Studio.
- Per-request dynamic KV-bits (load-time only).
