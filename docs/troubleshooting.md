# Troubleshooting

## `make validate` fails with "permission matrix drift"
One of `agents/*/permissions.yml` no longer matches DESIGN.md §5.2. Either fix the YAML or update `tools/check_permission_matrix.py::CANONICAL` and DESIGN.md together in the same PR.

## `bats tests/shell` fails "manifest not found"
You are running outside repo root. `cd` to the repo root; the suite uses relative paths.

## `verify-checksums.sh` reports mismatch
Either the model file is corrupt (re-download) or an unauthorized update happened. Do NOT auto-accept — investigate before updating the manifest.

## Hermes agent refuses a file
Hermes consults `security/file-access-rules.yml` at every read/write. Check the role's `deny:` and `write:` globs. Do not loosen rules without updating `tools/check_permission_matrix.py` and DESIGN.md.

## Cloud call fails for Tester / Sr Dev / Sr Arch
By design — only EM is allowed cloud inference. See `hermes/config.yml → inference.cloud.allowed_roles`.
