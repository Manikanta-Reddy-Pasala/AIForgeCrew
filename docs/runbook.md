# RUNBOOK

How to bring up, operate, and recover AIForgeCrew. macOS-only. Every step
scripted — no manual clicks inside configs.

---

## Pipeline v4.1 bring-up (2026-04-21)

Full v4.1 spec: [`docs/superpowers/specs/2026-04-21-autonomous-memory-orchestration-design.md`](./superpowers/specs/2026-04-21-autonomous-memory-orchestration-design.md)

### First-time install on Mac Studio

```bash
# 1. Postgres schema for the 4-tier memory store
bash scripts/install-pg-aiforge.sh

# 2. Embed sidecar (bge-m3 ONNX) on :8764
bash scripts/install-embed-sidecar.sh &

# 3. Rerank sidecar (bge-reranker-v2-m3) on :8765
bash scripts/install-rerank-sidecar.sh &

# 4. Graphify code knowledge graph (understanding layer)
bash scripts/install-graphify.sh

# 5. Seed codebase into T4
make migrate-memory    # or: python3 -m aiforge_core.cli memory reindex-code --repo aiforge
```

### Daily ops

```bash
# Rebuild graph after code changes
make graphify-rebuild

# Reindex T4 after significant edits
python3 -m aiforge_core.cli memory reindex-code --repo aiforge

# Review + approve semantic/procedural proposals from reflection
python3 -m aiforge_core.cli memory propose-list
python3 -m aiforge_core.cli memory propose-approve <id>
python3 -m aiforge_core.cli memory propose-reject  <id>

# Garbage-collect expired T1 rows (post-merge, 7-day TTL)
python3 -c "from aiforge_core.store_v2 import Store; print('gc:', Store().gc_expired())"
```

### Kill switch

Trip the emergency stop — orchestrator checks before every agent hop.

**Global kill (halts every ticket):**

```bash
ssh manikanta@192.168.70.185 'touch ~/AIForgeCrew/.aiforge/KILL'
# release
ssh manikanta@192.168.70.185 'rm ~/AIForgeCrew/.aiforge/KILL'
```

**Per-ticket kill (stops one ticket):** apply the `kill` label in Paperclip
UI, or via SQL:

```bash
KILL_LABEL_ID=d2e52007-ae22-4448-b952-f6176ee32e9c
TICKET_UUID=<issue-uuid>
ssh manikanta@192.168.70.185 "curl -s -X POST \
  http://localhost:3100/api/issues/$TICKET_UUID/labels \
  -H 'Content-Type: application/json' \
  -d '{\"labelId\":\"$KILL_LABEL_ID\"}'"
```

Remove by deleting the `kill` label from the ticket in the UI.

### 30B-only mode (no Claude cloud)

Run the Architect locally on gemma-4-31b-it instead of Claude Code:

```bash
export AIFORGE_ARCHITECT_MODE=local_30b
# Architect now uses agents/architect/system-prompt.local-30b.md
# with gemma-4-31b-it via LM Studio
```

Memory footprint (all-local, all models hot on M3 Ultra 96 GB):

| Slot          | Model                         | VRAM   |
|---------------|-------------------------------|--------|
| Architect     | gemma-4-31b-it (local_30b)    | ~20 GB |
| SrDev         | gemma-4-31b-it (same instance)| 0 GB  (shared) |
| Developer     | qwen3-coder-next (MoE 80B/3B) | ~45 GB |
| Fact Extract  | qwen3-4b-thinking-2507        | ~3 GB  |
| Embed sidecar | bge-m3 ONNX                   | ~2 GB  |
| Rerank sidecar| bge-reranker-v2-m3            | ~1 GB  |
| **Total**     |                               | ~71 GB (25 GB headroom) |

---

## Pipeline v4 operations (2026-04-20, superseded by v4.1 above)

Dispatcher scripts replace per-role daemons. Human (Architect) drives the loop.

### Ship a ticket end-to-end

```bash
# 1. Write Architect context (human step)
cp docs/tickets/TEMPLATE.md docs/tickets/ONE-NN.md
vim docs/tickets/ONE-NN.md

# 2. Create Paperclip ticket + branches across involved repos
# (manual via curl or Paperclip UI — eventually script this)

# 3. Full pipeline in one command (all phases + review loop, max 2 bounces)
bash scripts/ticket-run.sh ONE-NN

# OR per-phase
bash scripts/srdev-run.sh ONE-NN      # Sr Dev breakdown (gemma-4-31b)
bash scripts/dev-run.sh  ONE-NN       # Developer code + tests (qwen-coder-next)
bash scripts/review-run.sh ONE-NN     # Sr Dev review
bash scripts/bounce-run.sh ONE-NN     # Developer rework on review fail
```

Status transitions (enforced by dispatchers):
- `backlog` while any hermes run active → prevents Paperclip auto-retry
- `todo` after agent completes + dispatcher posts verdict
- `in_review` if review verdict = READY_FOR_REVIEW
- `blocked` if bounce cap exceeded

Markers in ticket comments:
- `READY_FOR_DEV` (Sr Dev → Developer)
- `READY_FOR_REVIEW` (Developer → human)
- `NEEDS_DEV_REWORK` (Review → Developer bounce)
- `NEEDS_HUMAN` (cap exceeded)

### Runtime tooling (verify after reboot)

```bash
# JDK + Maven + uv present in ssh PATH
ssh manikanta@192.168.70.185 'java --version && mvn --version | head -1 && uv --version'

# LM Studio parallel-load check (dispatchers auto-invoke ensure-model.sh)
ssh manikanta@192.168.70.185 '~/.lmstudio/bin/lms ps'

# Hermes context cache (should match actual loaded ctx)
ssh manikanta@192.168.70.185 'grep -E "gemma|qwen" ~/.hermes/context_length_cache.yaml'

# RAG CLI sanity
ssh manikanta@192.168.70.185 '~/.local/bin/rag -k 1 "atomic update pattern"'
```

### Context-related recovery (common LM Studio quirks)

Symptom: Developer/Review exit after ~2–15s with EXIT=1.

Check:
```bash
ssh manikanta@192.168.70.185 'curl -s http://localhost:1234/v1/models | python3 -m json.tool | head -20'
# Verify target model ID listed
ssh manikanta@192.168.70.185 '~/.lmstudio/bin/lms ps'
# If CONTEXT < 64K or model has `:N` suffix clone, dispatcher's ensure-model will clean up automatically
```

Force-reload manually:
```bash
bash scripts/lib/ensure-model.sh qwen3-coder-next 65536
bash scripts/lib/ensure-model.sh gemma-4-31b-it 65536
```

Hermes requires ≥ 64K ctx. LM Studio's RAM guardrail will silently reduce ctx — `ensure-model.sh` detects this + syncs Hermes cache to whatever LM Studio actually loaded.

### Rebuild RAG after doc edits

```bash
ssh manikanta@192.168.70.185 'cd ~/AIForgeCrew && .venv/bin/python scripts/rag-reindex-multi.py'
```

Indexes: AIForgeCrew docs + PosPythonBackend + TallyConnector + MongoDbService + PosDataSyncService. Java files chunked at method boundaries, markdown/etc at 2500-char windows.

### Re-seed repo inventory (after edits to docs/repo-inventory.md)

```bash
bash scripts/seed-repo-inventory.sh
# Seeds 44 world facts into Hindsight aiforge bank (one per codeRepo sibling).
# Idempotent: clears `repo-inventory` tagged rows then reinserts.
# Direct SQL — no LLM extraction, avoids NIM 429 + LM Studio response_format quirks.
```

### Paperclip agent state

```bash
# List agents
ssh manikanta@192.168.70.185 "PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"SELECT name, status, adapter_config->>'model' FROM agents WHERE company_id='fd294bd0-2f65-405f-b443-fb41d66226fb' ORDER BY status\""

# Active = Sr Developer (gemma-4-31b) + Developer (qwen-coder-next)
```

### Reset a stuck ticket

```bash
# Kill hermes + reset ticket
ssh manikanta@192.168.70.185 'pkill -9 -f "hermes chat" 2>/dev/null; sleep 2'
ssh manikanta@192.168.70.185 "PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"UPDATE issues SET status='todo' WHERE identifier='ONE-NN'\""

# Reset bounce counter (dispatcher counts comments matching BOUNCE_ROUND)
ssh manikanta@192.168.70.185 "PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"DELETE FROM issue_comments WHERE issue_id=(SELECT id FROM issues WHERE identifier='ONE-NN') AND body LIKE '%BOUNCE_ROUND%'\""
```

---

## Legacy bring-up (v2 flow — still valid for auxiliary install tasks)

## 0. First-time bring-up on a fresh Mac Studio

```bash
# 1. Install LM Studio from lmstudio.ai (one-time GUI install for `lms` CLI)
# 2. Install uv (single binary, no sudo, no Xcode CLT)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Prevent sleep during long downloads / bench runs
caffeinate -dimsu &

# 4. Clone this repo on the Mac Studio (or SSH-drive from another machine)
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew
cd AIForgeCrew

# 5. Install the Hermes-side Python runtime
make aiforge-install        # .venv + `aiforge` + `hermes` CLIs

# 6. Install ChromaDB + build initial RAG index
make rag-install

# 8. Pull models per security/model-checksums.yml (≈65 GB, ~2h on residential link)
make models                   # download + verify sha256 + start LM Studio + load + health

# 9. Install real Paperclip (UI on Mac Studio :3100)
make paperclip-install        # Node 20 via fnm + npx paperclipai onboard --yes
make paperclip-start          # server up
make paperclip-bootstrap      # create OneShell company + 4 agents (idempotent)

# 10. Sanity checks
make validate permission-check audit-tools
make aiforge-doctor
make hermes-test              # 7 hermes tests
.venv/bin/pytest tests/python/ -q     # 72 tests total

# 11. Live smoke
make paperclip-status
.venv/bin/aiforge ticket create --title "hello world" --body "ping"
.venv/bin/aiforge report-fleet | jq .

# 12. Open Paperclip UI from your laptop (keep-open)
make paperclip-tunnel         # ssh -L 3100; then open http://localhost:3100
```

## 1. Daily ops

### Reboot recovery

`caffeinate` + LM Studio server do not survive a Mac Studio reboot. Re-run:

```bash
caffeinate -dimsu &
make server                   # brings up :1234 + waits-ready
make load                     # loads 3 role models @ 128K context
make health                   # verifies per-role inference
```

### Check current state

```bash
make aiforge-doctor                           # config + DB sanity
.venv/bin/aiforge ticket list --state reviewing
.venv/bin/aiforge report-fleet | jq .         # fleet metrics
.venv/bin/aiforge report-ticket TICKET-xxx    # per-ticket drill-in
```

### Rebuild RAG after adding docs

```bash
ssh manikanta@192.168.70.185 'cd ~/AIForgeCrew && .venv/bin/python scripts/rag-reindex-multi.py'
```

## 2. Failure playbook

### SSH times out to Mac Studio

Usually sleep. On the Mac Studio (via Chrome Remote KVM):

```bash
caffeinate -dimsu &
ifconfig | awk '/^[a-z]/{iface=$1} /inet /&&!/127\.0\.0\.1/{print iface, $2}'
# confirm IP then update SSH_HOST= in the Makefile invocation
```

DHCP-reservation at the router is the permanent fix — see `project_p0_setup.md`
in memory for the MAC address.

### LM Studio doesn't accept a new model

```bash
make verify                   # re-checks all sha256
# If mismatch: `lms get` for the specific model resumes from partial
# If still bad: delete the model dir + re-run `make download`
```

### Agent budget blew up

```bash
.venv/bin/aiforge report-ticket TICKET-xxx | jq .tokens_per_role
.venv/bin/aiforge budget-report --role sr_developer
# If mid-run: the BudgetExceeded exception already halted the call; ticket
# stays in its current state. Reset by transitioning to `escalated` with
# actor=human and rerouting after fix.
```

### Circuit breaker tripped

```bash
# Look for the trip event
.venv/bin/aiforge audit TICKET-xxx | grep breaker
# Reset (requires human actor)
.venv/bin/python -c "
from pathlib import Path
from aiforge_core.store import Store
from aiforge_core.retry import CircuitBreaker
s = Store(Path('.paperclip/paperclip.db'))
CircuitBreaker(store=s).reset('TICKET-xxx', 'sr-developer', actor='human')
"
```

### Stale ticket > 1h with no activity

`fleet_summary.stalled_tickets` surfaces these. Default threshold = 60 min
(`retry_rules.stale_ticket_timeout_minutes` in `paperclip.config.yml`). Human
comments + reassigns or transitions to `escalated`.

## 3. Security checks (run before each release)

```bash
make validate                 # config schemas
make permission-check         # DESIGN §5.2 matrix vs YAML
make audit-tools              # no network-capable tools in any registry
make verify                   # all model sha256 match manifest
```

## 4. Full regression

```bash
make validate permission-check audit-tools
.venv/bin/pytest tests/python/ -v      # 72 tests
make bench                             # solo throughput (solo per role)
make bench-concurrent                  # paired throughput
make bench-passk                       # pass@1 on docs/eval/tickets
```

## 5. Where things live

| Path | Purpose |
|------|---------|
| `paperclip.config.yml` | Org chart, budgets, retry rules, routing |
| `agents/<role>/` | system-prompt.md, contract.md, permissions.yml |
| `security/model-checksums.yml` | Model path / URL / sha256 / role assignment |
| `security/file-access-rules.yml` | Per-role read/write globs (DESIGN §8.3) |
| `security/blocked-paths.yml` | Globally-blocked paths (.env, secrets/, .github/) |
| `.aiforge/mem/` | Palaces (gitignored): project + 4 per-role |
| `.aiforge/rag/` | ChromaDB RAG index (gitignored) |
| `.aiforge/crg/` | Cached call graph (gitignored) |
| `.paperclip/paperclip.db` | Ticket/comment/audit SQLite (gitignored) |
| `docs/eval/tickets/` | pass@1 evaluation set |
