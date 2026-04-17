# Security Policy

This is the human-readable companion to `security/file-access-rules.yml`, `security/blocked-paths.yml`, and `security/model-checksums.yml`. Any conflict is resolved in favor of the YAML files — they are the runtime source of truth.

## Principles (DESIGN.md §8.1)

1. Zero trust between agents.
2. Least privilege.
3. Prod secrets blocked for ALL agents.
4. Test secrets available to Tester only (read-only).
5. No network access for local agents.
6. No merge authority for any agent — humans only.
7. All inference local. Cloud use (EM only) carries ticket text, never code.

## File system sandbox

See `security/file-access-rules.yml` for the canonical globs.

## Hard-blocked paths

See `security/blocked-paths.yml` — enforced by CI and Hermes.

## Model integrity

Every model listed in `security/model-checksums.yml` must hash-match before startup. `scripts/verify-checksums.sh` is the gate.

## Reporting a vulnerability

Open a private security advisory:
https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew/security/advisories/new

Do NOT open a public issue.
