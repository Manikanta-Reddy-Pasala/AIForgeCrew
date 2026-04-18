# aiforge skill pack (for Hermes)

Markdown-based Hermes skills exposing `aiforge_core` policies to the agent turn.
Installed to `~/.hermes/skills/aiforge/` on the Mac Studio by
`scripts/install-aiforge-skills.sh`.

Every skill here is a thin wrapper around the `aiforge` CLI (see
`aiforge_core/cli.py`) and its Python modules. The install script rewrites the
embedded path placeholder `{{AIFORGE_BIN}}` to the actual binary location on
the target host.
