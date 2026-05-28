# tests/shell/test_scripts.bats
#!/usr/bin/env bats

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
}

@test "health-check.sh exists and is executable" {
  [ -x scripts/health-check.sh ]
}

@test "verify-checksums.sh exists and is executable" {
  [ -x scripts/verify-checksums.sh ]
}

@test "setup-models.sh exists and is executable" {
  [ -x scripts/setup-models.sh ]
}

@test "start-servers.sh exists and is executable" {
  [ -x scripts/start-servers.sh ]
}

@test "health-check.sh exits 0 when all probes skipped in dry-run" {
  run scripts/health-check.sh --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"OK"* ]]
}

@test "verify-checksums.sh exits 0 when models list is empty" {
  run scripts/verify-checksums.sh
  [ "$status" -eq 0 ]
}

@test "verify-checksums.sh exits non-zero when a declared model is missing" {
  tmp_manifest="$(mktemp -t manifest.XXXXXX.yml)"
  cat > "$tmp_manifest" <<EOF
version: 1
models:
  - name: nonexistent
    path: /tmp/definitely-not-there-$$.gguf
    sha256: 0000000000000000000000000000000000000000000000000000000000000000
    assigned_to: ["sr-developer"]
EOF
  run scripts/verify-checksums.sh --manifest "$tmp_manifest"
  [ "$status" -ne 0 ]
  rm -f "$tmp_manifest"
}

@test "setup-models.sh --dry-run prints planned actions" {
  run scripts/setup-models.sh --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"would download"* || "$output" == *"nothing to do"* ]]
}

@test "start-servers.sh --dry-run prints the compose command" {
  run scripts/start-servers.sh --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"docker compose"* ]]
}

@test "install-pg-aiforge.sh exists and is executable" {
  [ -x scripts/install-pg-aiforge.sh ]
}

@test "install-pg-aiforge.sh --dry-run prints CREATE EXTENSION statements" {
  run bash scripts/install-pg-aiforge.sh --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "CREATE EXTENSION IF NOT EXISTS vector" ]]
  [[ "$output" =~ "CREATE EXTENSION IF NOT EXISTS pg_trgm" ]]
  [[ "$output" =~ "CREATE TABLE memories" ]]
  [[ "$output" =~ "CREATE TABLE memory_proposals" ]]
}

@test "aiforge-graphify-all.sh exists and is executable" {
  [ -x scripts/runtime/aiforge-graphify-all.sh ]
}

@test "aiforge-graphify-all.sh --dry-run lists git repos under code root, skips non-git" {
  tmp="$(mktemp -d)"
  mkdir -p "$tmp/repoA/.git" "$tmp/repoB/.git" "$tmp/plaindir"
  run env AIFORGE_CODE_ROOT="$tmp" scripts/runtime/aiforge-graphify-all.sh --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"repoA"* ]]
  [[ "$output" == *"repoB"* ]]
  [[ "$output" != *"plaindir"* ]]
  rm -rf "$tmp"
}

@test "aiforge-graphify-all.sh --dry-run exits 0 when code root missing" {
  run env AIFORGE_CODE_ROOT="/no/such/path/xyz" scripts/runtime/aiforge-graphify-all.sh --dry-run
  [ "$status" -eq 0 ]
}

@test "aiforge-graphify-all.sh exits 0 when some repos fail but others succeed" {
  tmp="$(mktemp -d)"; mkdir -p "$tmp/repoA/.git" "$tmp/repoB/.git" "$tmp/bin"
  # fake graphify: succeed everywhere except when run inside repoB
  cat > "$tmp/bin/graphify" <<'FAKE'
#!/usr/bin/env bash
case "$PWD" in *repoB) exit 2 ;; esac
exit 0
FAKE
  chmod +x "$tmp/bin/graphify"
  run env AIFORGE_CODE_ROOT="$tmp" AIFORGE_HOME="$tmp/.aiforge" GRAPHIFY="$tmp/bin/graphify" \
      scripts/runtime/aiforge-graphify-all.sh
  [ "$status" -eq 0 ]
  rm -rf "$tmp"
}

@test "aiforge-graphify-all.sh exits 1 when every repo fails" {
  tmp="$(mktemp -d)"; mkdir -p "$tmp/repoA/.git" "$tmp/repoB/.git" "$tmp/bin"
  cat > "$tmp/bin/graphify" <<'FAKE'
#!/usr/bin/env bash
exit 2
FAKE
  chmod +x "$tmp/bin/graphify"
  run env AIFORGE_CODE_ROOT="$tmp" AIFORGE_HOME="$tmp/.aiforge" GRAPHIFY="$tmp/bin/graphify" \
      scripts/runtime/aiforge-graphify-all.sh
  [ "$status" -eq 1 ]
  rm -rf "$tmp"
}
