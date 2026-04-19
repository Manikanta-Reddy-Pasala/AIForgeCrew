# AIForgeCrew

Autonomous AI dev team. Human files a ticket in Paperclip → AI agents plan,
write tests first (TDD), implement, review, open MR. All threaded on one
ticket. Runs on a single Mac Studio (M3 Ultra, 96 GB) with a laptop as
remote control.

Architecture details: [`DESIGN.md`](./DESIGN.md). Ops: [`docs/runbook.md`](./docs/runbook.md).

## Three-layer stack (external tools + our policy layer)

| Layer | Component | What it does |
|---|---|---|
| Orchestrator | **Paperclip** (Node + React UI, github.com/paperclipai/paperclip) | Org chart, tickets, goals, runs, audit, dashboard. Embedded Postgres. |
| Agent runtime | **Hermes Agent** (Python CLI, github.com/NousResearch/hermes-agent) | Per-heartbeat LLM loop, 30+ tools, 80+ skills, session resume via `--resume`. |
| Policy layer | **aiforge_core** (this repo, Python) | DESIGN §4 lifecycle, §5 permissions, §8 security, §10 retry/coverage gate, §6 memory, §7 RAG + code-review-graph. Loaded into Hermes as a skill pack at `~/.hermes/skills/aiforge/`. |

Paperclip's `claude_local` adapter drives EM (Claude Code subscription).
`hermes_local` adapter drives the other 3 (local LM Studio → MLX models).

## Company + agents

**OneShell** — "Solving Business Problems with Software".

| Role | Paperclip adapter | Model | Where model runs |
|---|---|---|---|
| Engineering Manager | `claude_local` | `claude-opus-4-7` | Claude.ai subscription (OAuth) |
| Tester | `hermes_local` | `zai-org/glm-4.7-flash` | LM Studio :1234 (MLX) |
| Sr Developer | `hermes_local` | `qwen3.6-35b-a3b` | LM Studio :1234 (MLX) |
| Sr Architect | `hermes_local` | `gemma-4-31b-it` | LM Studio :1234 (MLX) |
| *-fallback | `hermes_local` | `minimax-ai/minimax-m2.7` (230B) | NVIDIA NIM (cloud, `NVIDIA_API_KEY`) |

Fallback fires after 2 dev↔tester (or dev↔architect) loops — one retry
below the DESIGN §10 hard escalate-to-human cap. Logic in
`aiforge_core.retry.pick_profile()`.

## Flow

```
Human
  │  creates ticket in Paperclip UI (http://paperclip.local)
  ▼
PAPERCLIP  (Node server, embedded Postgres, trusted-loopback)
  │  assigns to EM, heartbeat fires → claude_local adapter spawns Claude CLI
  ▼
[EM]            plans subtasks + acceptance criteria + test scenarios
                (ticket text scrubbed by aiforge_core.safety.scrub_ticket_text)
                comments on ticket, advances → tests_writing
  ▼
[Tester]        writes failing tests (Hermes → GLM-4.7-Flash @ LM Studio)
                git commit to tests/** via aiforge-git skill
                advances → coding
  ▼
[Sr Developer]  makes tests pass (Hermes → Qwen3.6-35B-A3B)
                git commit to src/** only (file ACL enforced)
                advances → verifying
  ▼
[Tester]        re-runs pytest, records `coverage` audit event (≥80 required)
                pass → reviewing ; fail → loop back to coding (max 3×)
  ▼
[Sr Architect]  reviews code + tests (Hermes → Gemma-4-31B, read-only)
                approve → mr_created, runs `gh pr create`
                reject → loop back to coding (max 3×)
  ▼
Human           merges MR
```

Loop caps + coverage gate are runtime-enforced by
`aiforge_core.lifecycle.advance()` via `retry.enforce_loop_caps` +
`retry.require_coverage_for_mr`. Every transition / tool call / budget
spend is append-only in `.paperclip/paperclip.db`.

## URLs (via Caddy reverse proxy)

After `make hosts-install` (edits `/etc/hosts` on both hosts + installs
Caddy as a LaunchDaemon on :80):

| Hostname | What | Maps to |
|---|---|---|
| `http://paperclip.local` | Paperclip UI + API | localhost:3100 on Mac Studio |
| `http://hermes.local` | Hermes web dashboard | localhost:9119 on Mac Studio |

Plain HTTP on LAN (no cert — `.local` names can't get Let's Encrypt).

Raw ports still work too: `:3100` and `:9119` on the Mac Studio.

## Auto-start on login

All services are LaunchAgents / LaunchDaemons so nothing needs babysitting:

| Service | Label | Survives reboot |
|---|---|---|
| `caffeinate -dimsu` | `com.aiforge.caffeinate` | ✓ user agent |
| LM Studio server + 3 role models loaded @ 128K | `com.aiforge.lmstudio` | ✓ user agent (one-shot) |
| Paperclip (`npx paperclipai run`) | `com.aiforge.paperclip` | ✓ user agent |
| Hermes dashboard (`hermes dashboard --port 9119`) | `com.aiforge.hermes-dashboard` | ✓ user agent |
| Caddy reverse proxy on :80 | `com.aiforge.caddy` | ✓ system daemon (root — needs sudo install) |

User agents run inside the GUI login session → inherit login keychain →
Claude Code subscription token is accessible to the `claude_local`
adapter's spawned children.

## Fresh Mac Studio bring-up

One-time on the Mac Studio itself (Chrome Remote Desktop → Terminal):

```bash
# 1. Xcode CLT (dialog — Install)
xcode-select --install

# 2. uv (Python bootstrap, no sudo)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. LM Studio.app — download .dmg from lmstudio.ai, drag to /Applications

# 4. Clone repo
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew
cd AIForgeCrew
```

Then from the laptop (everything else scripted):

```bash
# Core Python policy layer + tests + CLI
make aiforge-install

# Memory / RAG / CRG (optional)
make mempalace-install
make rag-install

# P0 models (~65 GB MLX download once)
make models

# External tools
make hermes-install                # real Hermes CLI (Python via uv)
make hermes-adapter-install        # hermes-paperclip-adapter (npm)
make paperclip-install             # Node 20 via fnm + npx paperclipai onboard
make paperclip-bootstrap           # create OneShell + 4 agents via REST
make paperclip-em-use-claude       # EM adapter → claude_local
make patch-hermes-adapter          # map local model prefixes → auto provider

# Claude CLI (EM's subscription) + login (interactive — must be done on Mac Studio)
make claude-cli-install
ssh -t manikanta@192.168.70.185 'claude /login'   # browser OAuth

# Fallback provider
NVIDIA_API_KEY=nvapi-... make hermes-configure

# Autostart + friendly URLs
make autostart-install
make hosts-install                 # laptop /etc/hosts + Caddy on Mac Studio
```

## Daily ops

```bash
# Status
make autostart-status
curl -s http://paperclip.local/api/health

# Open UI on laptop
open http://paperclip.local
open http://hermes.local

# Ticket via REST (Paperclip assigns by heartbeat; no CLI needed)
UUID=$(curl -s http://paperclip.local/api/companies | jq -r .[0].id)
EM=$(curl -s "http://paperclip.local/api/companies/$UUID/agents" | jq -r '.[] | select(.role=="pm") | .id')
curl -s -X POST "http://paperclip.local/api/companies/$UUID/issues" \
     -H 'Content-Type: application/json' \
     -d "{\"title\":\"Add foo()\",\"description\":\"...\",\"assigneeAgentId\":\"$EM\"}"

# Local aiforge CLI (policy + reports, mirrors Paperclip runs)
.venv/bin/aiforge report-ticket TICKET-xxx | jq .
.venv/bin/aiforge report-fleet              | jq .
.venv/bin/aiforge audit TICKET-xxx

# Rebuild RAG after doc edits
make rag-reindex
```

## Test harness

```bash
.venv/bin/pytest tests/python/ -q     # 72 tests, ~2s
make validate permission-check        # config schemas + DESIGN §5.2 matrix
make bench                            # per-role tok/s on LM Studio
make bench-passk                      # pass@1 on docs/eval/tickets/
```

## Command reference

`make help` — full list. Groups:

```
Dev             : setup, lint, test, validate, permission-check
P0 models       : models, download, verify, server, load, health,
                  bench, bench-concurrent, bench-passk
aiforge-core    : aiforge-install, aiforge-test, aiforge-doctor, aiforge -- ARGS
memory          : mempalace-install, mempalace-test, mempalace-index-all
rag + crg       : rag-install, rag-reindex, rag-query, crg-query
Paperclip       : paperclip-install, paperclip-start, paperclip-stop,
                  paperclip-status, paperclip-bootstrap, paperclip-em-use-claude,
                  paperclip-tunnel
Real Hermes     : hermes-install, hermes-adapter-install, hermes-configure,
                  hermes-skills-install, hermes-dashboard-start, -stop, -tunnel,
                  hermes-login, patch-hermes-adapter
Claude CLI      : claude-cli-install
Sync            : sync-memory-push, sync-memory-pull, sync-code-repos,
                  deploy-mac-studio
Autostart + DNS : autostart-install, autostart-uninstall, autostart-status,
                  caddy-install, hosts-install-laptop, hosts-install
```

## Repo layout

| Path | Purpose |
|------|---------|
| `aiforge_core/` | Python policy layer (lifecycle, store, mem, rag, crg, git_ops, safety, retry, observe, net, CLI) |
| `aiforge_core/skills/` | Hermes skill pack templates (installed to `~/.hermes/skills/aiforge/`) |
| `agents/<role>/` | system-prompt.md, contract.md, permissions.yml per DESIGN §3 |
| `security/` | File ACL rules, blocked paths, network allowlist, model checksums |
| `memory/` | MemPalace config |
| `mcp/` | MCP server manifests (tool contracts) |
| `scripts/` | All install / provision / benchmark scripts (macOS-only) |
| `tools/` | Schema validators, permission matrix check |
| `tests/python/` | 72 pytest tests |
| `docs/` | runbook, architecture, hardware-guide, model-evaluation, security-policy, troubleshooting, eval/tickets |

Runtime state (all gitignored):
- Laptop: `.paperclip/` (our SQLite mirror), `.aiforge/` (mem + rag + crg), `.venv/`
- Mac Studio: `~/.paperclip/instances/default/` (Paperclip + Postgres), `~/.hermes/` (Hermes sessions + skills), `~/codeRepo/` (mirrored repos), `~/.mempalace/`

## Key shortcuts

- **Paperclip UI:** http://paperclip.local
- **Hermes dashboard:** http://hermes.local
- **Mac Studio SSH:** `manikanta@192.168.70.185` (override `SSH_HOST=...`)
- **Everything scripted.** No manual config edits. No unscripted SSH commands.

## License

MIT — see [`LICENSE`](./LICENSE).
