.PHONY: help install test ui deploy pull cleanup-v4 kill-all \
        index-all status logs-tail health

# SSH target for Mac Studio. Override: `make X SSH_HOST=user@host`.
SSH_HOST ?= manikanta@192.168.70.185

help:
	@echo "Dev + deploy targets:"
	@echo "  install            .venv + uv pip install -e .[dev]"
	@echo "  test               pytest tests/python"
	@echo "  ui                 vite build (web/dist)"
	@echo "  deploy             git push + pull+build+restart on Mac Studio"
	@echo "  pull               git pull on Mac Studio"
	@echo "  cleanup-v4         legacy paperclip/hermes/hindsight purge (backed up)"
	@echo ""
	@echo "Ops:"
	@echo "  status             tickets + agents + health"
	@echo "  logs-tail          stream orchestrator ndjson logs (all roles)"
	@echo "  health             /api/health on Mac Studio"
	@echo "  sync-memory        rsync CLAUDE.md + ~/.claude/memory → Mac Studio + reindex"
	@echo "  reindex-memory     reindex-only (no rsync) — after commits land on Mac Studio"
	@echo "  index-all          bulk re-index all ~/codeRepo trees into T4"
	@echo "  kill-all           launchctl bootout every com.aiforge.* agent"

install:
	uv venv .venv
	.venv/bin/uv pip install -e ".[dev]"

test:
	.venv/bin/pytest tests/python -v

ui:
	cd web && npm install && npm run build

deploy:
	git push origin main
	ssh $(SSH_HOST) 'cd ~/AIForgeCrew && git pull && cd web && npm run build && launchctl kickstart -k gui/$$(id -u)/com.aiforge.api'

pull:
	ssh $(SSH_HOST) 'cd ~/AIForgeCrew && git pull'

cleanup-v4:
	ssh $(SSH_HOST) 'cd ~/AIForgeCrew && bash scripts/runtime/cleanup-v4.sh'

status:
	ssh $(SSH_HOST) 'curl -s http://127.0.0.1:8799/api/health; echo; curl -s http://127.0.0.1:8799/api/tickets?limit=15 | head -c 2000'

logs-tail:
	ssh $(SSH_HOST) "tail -f ~/.aiforge/logs/orchestrator-*.ndjson | jq -c '{ts,role,ticket,event,tool,turn,stop_reason}'"

health:
	ssh $(SSH_HOST) 'curl -s http://127.0.0.1:8799/api/health'

sync-memory:
	SSH_HOST=$(SSH_HOST) bash scripts/sync-memory.sh

reindex-memory:
	SSH_HOST=$(SSH_HOST) REINDEX_ONLY=1 bash scripts/sync-memory.sh

index-all:
	ssh $(SSH_HOST) 'cd ~/AIForgeCrew && bash scripts/runtime/embed-backfill.py --limit 5000'

kill-all:
	ssh $(SSH_HOST) 'for r in architect sr_developer developer fact_extract; do launchctl bootout gui/$$(id -u)/com.aiforge.tick-$$r 2>/dev/null || true; done; launchctl bootout gui/$$(id -u)/com.aiforge.api 2>/dev/null || true; echo stopped'

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ build dist *.egg-info
