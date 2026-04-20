# AIForgeCrew

Autonomous AI dev team. Human files a ticket in Paperclip → AI agents plan, implement, review, open PR. All threaded on one ticket. Runs on a single Mac Studio (M3 Ultra, 96 GB) with a laptop as remote control.

Architecture details: [`DESIGN.md`](./DESIGN.md) · Memory system: [`docs/agents/memory-system.md`](./docs/agents/memory-system.md) · Ops: [`docs/runbook.md`](./docs/runbook.md)

## Three-layer stack

| Layer | Component | What it does |
|---|---|---|
| Orchestrator | **Paperclip** (Node + React UI, `github.com/paperclipai/paperclip`) | Org chart, tickets, goals, runs, audit, dashboard. Embedded Postgres. |
| Agent runtime | **Hermes Agent** (Python CLI, `github.com/NousResearch/hermes-agent`) | Per-turn LLM loop, 30+ tools, 80+ skills, session resume. |
| Policy layer | **aiforge_core** (this repo) | Lifecycle, permissions, RAG, memory, dispatcher scripts. Installed as Hermes skill pack at `~/.hermes/skills/aiforge/`. |

## Pipeline v4 (2026-04-20) — Architect / Sr Dev / Developer

| Role | Who | Model | Job |
|---|---|---|---|
| **Software Architect** | Claude Code (external, this session) | Opus 4.7 cloud | Writes `docs/tickets/<TICKET-ID>.md` with design choice + acceptance criteria. Creates `aiforge/<TICKET-ID>` branch per repo. |
| **Sr Developer** | Paperclip agent `28b8c064` | `gemma-4-31b-it` @ 64K (LM Studio MLX) | Reads context, recalls memory + RAG, writes `docs/breakdowns/<TICKET-ID>.md` with numbered sub-tasks + test spec. Also runs **REVIEW mode** after Developer commits. |
| **Developer** | Paperclip agent `e0502e94` | `qwen3-coder-next` @ 64K (LM Studio MLX) | Implements each sub-task + unit test. `mvn compile` + `mvn test` must pass. Commits per sub-task. Pushes. `gh pr create`. |

Paused roles (kept for audit): Engineering Manager, Sr Architect, Tester.

Branch convention: `aiforge/<TICKET-ID>` — same name across every involved repo.

Handoff markers (in ticket comments): `READY_FOR_DEV`, `READY_FOR_REVIEW`, `NEEDS_DEV_REWORK`, `NEEDS_HUMAN`.

Max 2 bounce cycles. Exceeded → status `blocked`.

## Pipeline flow

```
Human              Architect (me)          Sr Dev (gemma)       Developer (qwen)      Sr Dev review
  │                    │                       │                     │                     │
  │ ticket idea        │                       │                     │                     │
  ├───────────────────►│ writes docs/tickets   │                     │                     │
  │                    │ /<TICKET>.md          │                     │                     │
  │                    │ creates branches      │                     │                     │
  │                    │ across repos          │                     │                     │
  │                    │                       │                     │                     │
  │                    │ bash srdev-run.sh ────►│ rag + hindsight    │                     │
  │                    │                       │ → docs/breakdowns/ │                     │
  │                    │                       │ → READY_FOR_DEV     │                     │
  │                    │                       │                     │                     │
  │                    │ bash dev-run.sh ───────────────────────────►│ impl + unit test    │
  │                    │                       │                     │ mvn compile + test  │
  │                    │                       │                     │ git push + gh pr    │
  │                    │                       │                     │ → READY_FOR_REVIEW  │
  │                    │                       │                     │                     │
  │                    │ bash review-run.sh ────────────────────────────────────────────►│ verify diff
  │                    │                       │                     │                     │ mvn test
  │                    │                       │                     │                     │ verdict
  │                    │                       │                     │                     │
  │                    │◄───────────────────────── if NEEDS_DEV_REWORK (≤2×)                │
  │                    │ bounce-run.sh                                                      │
  │                    │                                                                    │
  │◄─────────────── human review + merge PR                           (if READY_FOR_REVIEW) │
```

## Dispatcher scripts

| Script | Role | Model | Key guards |
|---|---|---|---|
| `scripts/srdev-run.sh <TICKET>` | Sr Dev breakdown | gemma-4-31b @ 64K | mandates hindsight+rag+≤3 file reads |
| `scripts/dev-run.sh <TICKET>` | Developer impl | qwen-coder-next @ 64K | mvn compile + mvn test -Dtest=<X> required; auto `gh pr create` |
| `scripts/review-run.sh <TICKET>` | Sr Dev review | gemma-4-31b @ 64K | git diff + mvn check; emits `VERDICT_START..VERDICT_END` block |
| `scripts/bounce-run.sh <TICKET>` | Developer rework | qwen-coder-next @ 64K | feeds review verdict back; cap 2 per ticket |
| `scripts/ticket-run.sh <TICKET>` | Full pipeline | — | runs all 4 phases w/ review loop |

Supporting:
- `scripts/lib/ensure-model.sh <MODEL> <CTX>` — guards against LM Studio silent ctx reduction + JIT-loaded `:N` clones. Syncs Hermes ctx cache. REST smoke test.
- `scripts/rag "<query>"` — CLI wrapper for aiforge-rag (ChromaDB over AIForgeCrew + OneShell repos, method-boundary chunked).
- `scripts/rag-reindex-multi.py` — rebuilds vector index across all repos.

All dispatchers:
- Set ticket status to `backlog` during hermes run (prevents Paperclip auto-retry interference)
- Post verdict comments via curl themselves (not trusting agents to POST)
- Restore status to `todo` / `in_review` / `blocked` based on outcome

## Company + agents

**OneShell** — "Solving Business Problems with Software".

Active:
| Role | Agent ID | Adapter | Model |
|---|---|---|---|
| Sr Developer | `28b8c064-bfcf-44e1-9e91-e37c39e0097c` | `hermes_local` | `gemma-4-31b-it` |
| Developer | `e0502e94-0608-4fb9-9afa-b70d8dbf014a` | `hermes_local` | `qwen3-coder-next` |

Paused (history retained):
| Role | Agent ID | Original model |
|---|---|---|
| Engineering Manager | `35760e2f-4cef-4013-9aff-d93592b5f71e` | `claude-opus-4-7` |
| Sr Architect | `0e173374-287c-4595-bf46-6ba26c11035f` | `gemma-4-26b-a4b-it` |
| Tester | `eb1c388d-8601-4df4-89d8-447ec2ff5946` | `qwen3.5-9b-mlx` |

Company ID: `fd294bd0-2f65-405f-b443-fb41d66226fb`.

## Runtime tooling on Mac Studio

Installed 2026-04-20 via `brew`:
- OpenJDK 25 (`/opt/homebrew/Cellar/openjdk/25.0.2/.../Contents/Home`)
- Maven 3.9.15 (`/opt/homebrew/Cellar/maven/3.9.15/libexec/bin/mvn`)
- `uv` 0.11.7 for Python (`~/.local/bin/uv`)

`JAVA_HOME` + `MAVEN_HOME` + `PATH` exported in `~/.zshrc`, `~/.zshenv`, `~/.profile`, `~/.bashrc` — visible in every ssh session.

LM Studio + MLX models:
- `gemma-4-31b-it` (dense 31B, 18 GB weights, 4-bit MLX)
- `qwen3-coder-next` (MoE 80B / 3B active, 44.86 GB)
- Both kept with `--ttl 86400` (24h, prevents LM Studio auto-evict)
- 96GB Mac Studio cannot keep both at 64K+ ctx simultaneously → dispatcher swaps per phase via `ensure-model.sh`

Hermes config (`~/.hermes/config.yaml`):
- `model.default = qwen3-coder-next` (so aux/vision probes hit a loaded model)
- `auxiliary.compression.model = qwen3-coder-next` (aux uses local, not cloud)
- `compression.enabled = false` + `threshold = 0.99` (belt + suspenders)

## Memory system — 6 layers

| # | Layer | Source | Per |
|---|---|---|---|
| 1 | System prompt | `~/.paperclip/.../agents/<id>/instructions/AGENTS.md` | agent |
| 2 | Ticket context | `docs/tickets/<TICKET-ID>.md` | ticket (Architect writes) |
| 3 | Breakdown | `docs/breakdowns/<TICKET-ID>.md` | ticket (Sr Dev writes) |
| 4 | Codebase RAG | ChromaDB via `rag` CLI | shared (multi-repo, method-boundary chunked) |
| 5 | Fact memory | Hindsight pgvector (per-agent bank planned) | agent |
| 6 | Git history | native git | shared |

Full design: [`docs/agents/memory-system.md`](./docs/agents/memory-system.md)

## URLs (via Caddy reverse proxy on Mac Studio)

| Hostname | What |
|---|---|
| `http://paperclip.local` | Paperclip UI + REST API |
| `http://hermes.local` | Hermes web dashboard |
| Raw ports | `localhost:3100` (Paperclip), `localhost:9119` (Hermes), `localhost:1234` (LM Studio) |

## Daily ops

```bash
# Status
make autostart-status
curl -sf http://paperclip.local/api/health

# Write an Architect ticket
vim docs/tickets/ONE-NN.md        # follow docs/tickets/TEMPLATE.md
# Create Paperclip ticket + branch via your preferred method

# Run full pipeline (or per phase)
bash scripts/ticket-run.sh ONE-NN
# OR
bash scripts/srdev-run.sh ONE-NN          # just breakdown
bash scripts/dev-run.sh  ONE-NN           # just impl
bash scripts/review-run.sh ONE-NN         # just review
bash scripts/bounce-run.sh ONE-NN         # rework after NEEDS_DEV_REWORK

# RAG query
~/.local/bin/rag "atomic update pattern" -k 5

# Rebuild RAG after doc edits (Mac Studio)
ssh manikanta@192.168.70.185 'cd ~/AIForgeCrew && .venv/bin/python scripts/rag-reindex-multi.py'
```

## Fresh Mac Studio bring-up

One-time on Mac Studio (Chrome Remote Desktop → Terminal):

```bash
# 1. Xcode CLT
xcode-select --install

# 2. Homebrew (not in default PATH previously)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 3. JDK + Maven + uv
/opt/homebrew/bin/brew install openjdk maven
curl -LsSf https://astral.sh/uv/install.sh | sh

# 4. Export env persistently (all 4 shell init files)
for f in ~/.zshrc ~/.zshenv ~/.profile ~/.bashrc; do
  cat >> $f <<'EOF'
export JAVA_HOME=/opt/homebrew/Cellar/openjdk/25.0.2/libexec/openjdk.jdk/Contents/Home
export MAVEN_HOME=/opt/homebrew/Cellar/maven/3.9.15/libexec
export PATH=$JAVA_HOME/bin:$MAVEN_HOME/bin:/opt/homebrew/bin:$PATH
EOF
done

# 5. LM Studio.app — drag to /Applications, launch once (for server init)

# 6. Clone repos
mkdir -p ~/codeRepo && cd ~/codeRepo
for r in PosPythonBackend MongoDbService TallyConnector PosServerBackend PosDataSyncService; do
  git clone https://github.com/OneShellSolutions/$r
done
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew
```

Then from the laptop:

```bash
make aiforge-install           # Python policy layer
make rag-install               # ChromaDB + deps
make models                    # ~65 GB MLX download (one-time)
make hermes-install            # real Hermes CLI via uv
make paperclip-install         # Paperclip via npx
make autostart-install         # LaunchAgents
```

## Test harness

```bash
.venv/bin/pytest tests/python/ -q     # policy-layer tests (~72 pass, ~2s)
make validate permission-check         # config schemas + DESIGN §5.2 matrix
make bench                             # per-role tok/s
```

## Repo layout

| Path | Purpose |
|------|---------|
| `aiforge_core/` | Python policy layer (lifecycle, store, mem, rag, CLI) |
| `aiforge_core/skills/` | Hermes skill pack (installed to `~/.hermes/skills/aiforge/`) |
| `agents/<role>/` | system-prompt.md, contract.md, permissions.yml |
| `docs/agents/` | CODEBASE_INDEX.md, memory-system.md, role-redesign-plan.md, engineer-split.md |
| `docs/tickets/` | Architect-written per-ticket context bundles (+ `TEMPLATE.md`) |
| `docs/breakdowns/` | Sr Dev-written per-ticket sub-task plans |
| `docs/eval/` | Bench CSVs, analysis reports |
| `scripts/` | Dispatchers: `srdev-run.sh`, `dev-run.sh`, `review-run.sh`, `bounce-run.sh`, `ticket-run.sh`; `lib/ensure-model.sh`; `rag`, `rag-cli.py`, `rag-reindex-multi.py`; install + bench scripts |
| `scripts/archive/` | Retired v3 dispatchers + per-model bench harnesses |
| `security/` | File ACL rules, blocked paths, network allowlist, model checksums |
| `memory/` | MemPalace config (legacy) |
| `mcp/` | MCP server manifests |
| `tests/python/` | 72 pytest tests |
| `docs/` | runbook, architecture, hardware-guide, model-evaluation, security-policy, troubleshooting |

Runtime state (gitignored):
- Laptop: `.paperclip/`, `.aiforge/`, `.venv/`
- Mac Studio: `~/.paperclip/instances/default/` (Paperclip + Postgres), `~/.hermes/` (sessions + skills), `~/codeRepo/` (mirrored repos)

## Key shortcuts

- **Paperclip UI:** http://paperclip.local
- **Hermes dashboard:** http://hermes.local
- **Mac Studio SSH:** `manikanta@192.168.70.185` (override `SSH_HOST=...`)
- **Everything scripted.** No manual config edits. No unscripted SSH commands.

## License

MIT — see [`LICENSE`](./LICENSE).
