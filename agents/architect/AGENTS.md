# Architect Role Pointer

The Architect role supports two modes, selected via `AIFORGE_ARCHITECT_MODE`:

| Mode       | Model               | Prompt file                    | Default? |
|------------|---------------------|--------------------------------|----------|
| `cloud`    | Claude Code         | `system-prompt.md`             | yes      |
| `local_30b`| `gemma-4-31b-it`    | `system-prompt.local-30b.md`   | no       |

Switch via `AIFORGE_ARCHITECT_MODE=local_30b make <target>` (or set in your shell).

The local-30b prompt is simplified and templated for deterministic output from a 30B dense model. The cloud prompt expects Claude-class reasoning.
