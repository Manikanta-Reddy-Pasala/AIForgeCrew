# Troubleshooting

## `make validate` fails with "permission matrix drift"
One of `agents/*/permissions.yml` no longer matches DESIGN.md §5.2. Either fix the YAML or update `tools/check_permission_matrix.py::CANONICAL` and DESIGN.md together in the same PR.

## `bats tests/shell` fails "manifest not found"
You are running outside repo root. `cd` to the repo root; the suite uses relative paths.

## `verify-checksums.sh` reports mismatch
Either the model file is corrupt (re-download) or an unauthorized update happened. Do NOT auto-accept — investigate before updating the manifest.

## Hermes agent refuses a file
Hermes consults `security/file-access-rules.yml` at every read/write. Check the role's `deny:` and `write:` globs. Do not loosen rules without updating `tools/check_permission_matrix.py` and DESIGN.md.

## SSH to Mac Studio times out (was working earlier)
Usually the Mac Studio went to sleep and/or the DHCP lease rolled over.
1. Wake via Chrome Remote Desktop.
2. On the Mac: `caffeinate -dimsu &`.
3. `ifconfig` and grab the new `en1` inet address.
4. Use `make SSH_HOST=user@NEW_IP <target>` or reserve the IP at the router.

## Hermes exits after 2-15s with EXIT=1
See runbook §"Context-related recovery". Root cause usually:
- LM Studio silently loaded model at 4K ctx instead of 64K (RAM guardrail)
- Hermes `context_length_cache.yaml` drifted from actual loaded ctx
- `model:N` JIT clone created alongside main
Run `bash scripts/lib/ensure-model.sh <MODEL> 65536` to reset.

## `lms load` hangs at 0% CPU
LM Studio's `lms load` wants a TTY for the progress bar. Over a plain SSH
call it stalls. Skip explicit loading — LM Studio auto-loads on the first
`/v1/chat/completions` for a model. `make health` triggers that.

## Python dies with `xcode-select: note: No developer tools were found`
Your host doesn't have Xcode CLT and something tried to use system
`/usr/bin/python3`. Every AIForgeCrew script uses `uv run --with pyyaml
python` to avoid this. If a new script breaks, port it to the same pattern.

## MemPalace verify-checksums complains about `globally_blocked`
`paperclip/permissions.py` accepts both `globally_blocked` (current key
in `security/blocked-paths.yml`) and legacy `blocked_paths`. If the manifest
drifts, fix it there — don't change the permission module.

## Circuit breaker tripped — test after fix
See `docs/runbook.md` §2 "Circuit breaker tripped" for the human-only reset.

