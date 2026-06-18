# KV-Quant Validation

> **STATUS 2026-06-19 — BLOCKED on LM Studio.** Live check on Mac Studio:
> `lms load --kv-cache-quantization N` → `error: unknown option`. The
> installed `lms` CLI exposes no KV-quant flag (only `--gpu
> --context-length --parallel --ttl --identifier --estimate-only`), no
> KV-quant config is persisted in `~/.lmstudio` config files, and
> standalone `mlx_lm` is not installed (LM Studio bundles its own MLX).
> KV-quant is therefore **default OFF** (`AIFORGE_LMS_KV_BITS=0`,
> `KV_BITS=0`). To actually realise it, pick one:
>   1. **mlx_lm.server runtime** — `pip install mlx-lm`, serve with
>      `--kv-bits 4 --quantized-kv-start N` (real native KV-quant; drops
>      LM Studio's model mgmt/GUI/TTL).
>   2. **LM Studio GUI** — set K/V Cache Quantization on the model load
>      panel (manual, not scriptable; verify the MLX engine honours it
>      and that it actually lowers RAM before relying on it).
> The procedure below applies once a working mechanism is enabled.

Goal: confirm 4-bit KV-quant on text models is quality-neutral + cuts RAM,
and that vision models still load.

## Mechanism (where it's wired)
- Runner auto-start: `aiforge_core/runtime/local_starter.py` —
  `AIFORGE_LMS_KV_BITS` (default 4) appends `--kv-cache-quantization <bits>`
  to `lms load` for **text** models only (`_model_kind`).
- Manual bulk load: `scripts/load-models.sh` — `KV_BITS` (default 4) does
  the same per role-model, skipping vision/embedding kinds.
- Exact `lms` flag token: confirm with `lms load --help` on Mac Studio;
  it is centralised as `_KV_FLAG` (local_starter) / `KV_FLAG`
  (load-models.sh) — one-line change if the version differs.

## Procedure (per text model, on Mac Studio)
1. Baseline: `AIFORGE_LMS_KV_BITS=0` load; record RAM (`lms ps` / Activity
   Monitor) at the 85K-ctx ceiling probe (reuse the obs-28564 long prompt).
2. Quant: `AIFORGE_LMS_KV_BITS=4` reload; rerun the SAME 85K probe.
3. Compare: output semantically equivalent? RAM lower? Record both.

## Vision regression guard
- Load a vision model (e.g. `nex-n2-mini`) via `load-models.sh` →
  confirm it loads cleanly (no obs-28582 crash) and KV-quant was skipped
  (the load log shows `kv=none` / no `--kv-cache-quantization`).

## Pass criteria
- Text: output equivalent, RAM measurably lower at 85K ctx.
- Vision: loads, KV-quant skipped.
