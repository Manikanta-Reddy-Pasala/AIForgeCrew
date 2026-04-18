# AIForgeCrew

Autonomous AI development team. Human creates a ticket; AI agents plan, write tests first (TDD), implement, review, and raise an MR — all threaded under the same ticket.

See [`DESIGN.md`](./DESIGN.md) for the complete architecture.

## Status

Phases P0 – P9 complete. All runtime + CLI + tests ship in this repo.
P10 (blog post + demo video) is content work; see `docs/runbook.md` for ops.

| Phase | Deliverable | Shipped |
|-------|-------------|---------|
| P0 | LM Studio + MLX models (Qwen3.6 / GLM-4.7 / Gemma-4) | ✅ |
| P1 | Paperclip runtime (tickets / lifecycle / budget / CLI) | ✅ |
| P2 | Hermes (agent driver, tool registry, LLM client) | ✅ |
| P3 | MemPalace two-tier memory with ACL | ✅ |
| P4 | RAG (ChromaDB) + code-review-graph (AST) | ✅ |
| P5 | Git MCP + end-to-end TDD integration test | ✅ |
| P6 | Prompt-injection scrub + network-tool audit | ✅ |
| P7 | Observability (per-ticket + fleet reports) | ✅ |
| P8 | Retry caps + circuit breaker + coverage gate | ✅ |
| P9 | pass@1 harness + 3 seed eval tickets | ✅ |
| P10 | `docs/runbook.md` (ops manual) | ✅ |

Bring-up on a fresh Mac Studio is `make paperclip-install && make mempalace-install && make rag-install && make models`. Full command reference: `make help`.

See [`docs/runbook.md`](./docs/runbook.md) for daily ops + failure playbook,
[`docs/superpowers/plans/`](./docs/superpowers/plans/) for detailed implementation plans.

## P0 model pipeline (reproducible)

One host (Mac Studio M3 Ultra 96 GB) runs LM Studio MLX. Any dev machine
drives download/verify/benchmark via SSH. Prereqs on the Mac Studio:
LM Studio installed (ships `~/.lmstudio/bin/lms` CLI) and [`uv`](https://docs.astral.sh/uv/)
for Python-without-Xcode.

Host target is `SSH_HOST=manikanta@192.168.70.185` by default; override
at invocation: `make models SSH_HOST=user@host`.

| Target | Action |
|--------|--------|
| `make models` | End-to-end: download + verify + server + load + health |
| `make download` | Idempotent fetch (reads `security/model-checksums.yml`) |
| `make verify` | sha256 verify every entry |
| `make server` | Start LM Studio OpenAI-compat server on :1234 |
| `make load` | Load role models with 128K context |
| `make health` | Probe `/v1/models` + one chat/completion per role |
| `make bench` | Solo per-role benchmark |
| `make bench-concurrent` | Paired concurrent throughput bench |

Every model entry in `security/model-checksums.yml` carries: path, `source_url`,
sha256, role assignment, quant, size, and rationale. Re-provisioning a fresh
Mac Studio from scratch is:

```bash
# on Mac Studio (one-time)
curl -LsSf https://astral.sh/uv/install.sh | sh
# install LM Studio from lmstudio.ai, run once to bootstrap lms CLI

# from any dev machine
make models       # pulls + verifies + starts + loads + health
```

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
