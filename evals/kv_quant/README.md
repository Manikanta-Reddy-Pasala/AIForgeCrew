# KV-Quant Validation

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
