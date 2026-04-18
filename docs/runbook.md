# RUNBOOK

How to bring up, operate, and recover AIForgeCrew. macOS-only. Every step
scripted — no manual clicks inside configs.

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

# 6. Install MemPalace + init 5 palaces (project + 4 roles)
make mempalace-install

# 7. Install ChromaDB + build initial RAG index
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
make rag-reindex
```

### Add a memory manually (project scope — EM / Architect only)

```bash
.venv/bin/python -c "
from pathlib import Path
from aiforge_core.mem import MemBus
MemBus(Path('.aiforge/mem')).remember(
    role='sr-architect',
    scope='project',
    text='Adopted Qwen3.6 for Dev role — see docs/model-evaluation.md',
    title='Model decision 2026-04-19',
)
"
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

### Coverage gate blocks MR (<80%)

Tester re-runs + records the new coverage:

```bash
# from Tester context (automated by the agent; here the manual override):
.venv/bin/python -c "
from pathlib import Path
from aiforge_core.store import Store
s = Store(Path('.paperclip/paperclip.db'))
s.audit_event('TICKET-xxx', 'coverage', 'tester', {'pct': 87.0, 'pass': 14, 'total': 14})
"
```

The architect's transition to `mr_created` will then succeed.

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
| `memory/mem0-config.yml` | Two-tier memory config (MemPalace) |
| `.aiforge/mem/` | Palaces (gitignored): project + 4 per-role |
| `.aiforge/rag/` | ChromaDB RAG index (gitignored) |
| `.aiforge/crg/` | Cached call graph (gitignored) |
| `.paperclip/paperclip.db` | Ticket/comment/audit SQLite (gitignored) |
| `docs/eval/tickets/` | pass@1 evaluation set |
