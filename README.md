# AIForgeCrew

Autonomous AI development team. Human creates a ticket; AI agents plan, write tests first (TDD), implement, review, and raise an MR — all threaded under the same ticket.

See [`DESIGN.md`](./DESIGN.md) for the complete architecture.

## Status

Phase 0 (hardware) / Phase 1 (scaffolding) in progress. See [`docs/superpowers/plans/`](./docs/superpowers/plans/) for implementation plans.

### Local automation

Validation + tests run locally via `make`:

| Target | Purpose |
|--------|---------|
| `make validate` | JSON-schema validation of every config |
| `make permission-check` | Enforce DESIGN.md §5.2 permission matrix |
| `make test` | `bats tests/shell` + `pytest tests/python` |
| `make lint` | yamllint + markdownlint + shellcheck (install-dependent) |

No CI pipeline. Commit gate is local `make validate permission-check test` before push.

## Quickstart

Prerequisites: Docker, Python 3.11+, Node 20+, `bats-core`, `shellcheck`, `yamllint`, `markdownlint-cli2`.

```bash
make setup       # install Python + Node tooling
make validate    # validate all configs against schemas
make lint        # yamllint + markdownlint + shellcheck
make test        # bats + pytest
```

## Repo Layout

| Path | Purpose |
|------|---------|
| `agents/` | Per-role system prompts, contracts, permissions |
| `security/` | File-access rules, blocked paths, model checksums |
| `hermes/` | Hermes agent runtime config + skills |
| `memory/` | Mem0 config, project memory, agent schemas |
| `rag/` | RAG indexing config and sources |
| `mcp/` | MCP server manifests |
| `observability/` | Dashboard + alert configs |
| `scripts/` | Setup, start, health-check, checksum-verify scripts |
| `tools/` | Schema validators, permission matrix check |
| `tests/` | bats (shell) + pytest (validator) tests |
| `docs/` | Hardware, model-evaluation, security policy, troubleshooting |
| `.github/` | Issue/PR templates, CODEOWNERS |

## License

MIT — see [`LICENSE`](./LICENSE).
