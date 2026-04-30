.PHONY: help install test ui deploy pull kill-all \
        index-all status logs-tail health sync-memory reindex-memory \
        test-codemem-L1 test-codemem-L2 test-codemem-L3 test-codemem-L4 \
        test-codemem-L5 test-codemem-L6 test-codemem-L7 test-codemem-all

# SSH targets.
#   MS_HOST: Mac Studio — runs graph-runner + LM Studio + embed sidecar
#   NUC_HOST: NUC — runs API, Neo4j, Postgres, indexers
# Override on the CLI e.g. `make status NUC_HOST=user@host`.
MS_HOST  ?= manikanta@192.168.70.185
NUC_HOST ?= mani@192.168.70.191
# Legacy single-host reference (kept for ssh targets that still run on MS).
SSH_HOST ?= $(MS_HOST)

help:
	@echo "Dev + deploy targets:"
	@echo "  install            .venv + uv pip install -e .[dev]"
	@echo "  test               pytest tests/python"
	@echo "  ui                 vite build (web/dist)"
	@echo "  deploy             git push + pull+build+restart on Mac Studio"
	@echo "  pull               git pull on Mac Studio"
	@echo ""
	@echo "Ops:"
	@echo "  status             tickets + agents + health"
	@echo "  logs-tail          stream orchestrator ndjson logs (all roles)"
	@echo "  health             /api/health on Mac Studio"
	@echo "  sync-memory        commit + push ~/.claude/memory to the shared github repo"
	@echo "  reindex-memory     pull memory on NUC + rebuild its affected wings"
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
	ssh $(MS_HOST)  'cd ~/AIForgeCrew && git pull'
	ssh $(NUC_HOST) 'cd ~/AIForgeCrew && git pull && systemctl --user restart aiforge-api'

test-codemem-L1:
	.venv/bin/pytest aiforge_core/codemem/tests/L1_repo_node/ -v

test-codemem-L2:
	.venv/bin/pytest aiforge_core/codemem/tests/L2_service_extract/ -v

test-codemem-L3:
	.venv/bin/pytest aiforge_core/codemem/tests/L3_file_summary/ -v

test-codemem-L4:
	.venv/bin/pytest aiforge_core/codemem/tests/L4_symbols/ -v

test-codemem-L5:
	.venv/bin/pytest aiforge_core/codemem/tests/L5_chunks_vectors/ -v

test-codemem-L6:
	.venv/bin/pytest aiforge_core/codemem/tests/L6_translator/ -v

test-codemem-L7:
	.venv/bin/pytest aiforge_core/codemem/tests/L7_bundle/ -v

test-codemem-all:
	.venv/bin/pytest aiforge_core/codemem/tests/ -v

pull:
	ssh $(MS_HOST)  'cd ~/AIForgeCrew && git pull'
	ssh $(NUC_HOST) 'cd ~/AIForgeCrew && git pull'

status:
	ssh $(NUC_HOST) 'curl -s http://127.0.0.1:8799/api/health; echo; curl -s http://127.0.0.1:8799/api/tickets?limit=15 | head -c 2000'

logs-tail:
	ssh $(MS_HOST) "tail -f ~/.aiforge/logs/orchestrator-*.ndjson | jq -c '{ts,role,ticket,event,tool,turn,stop_reason}'"

health:
	ssh $(NUC_HOST) 'curl -s http://127.0.0.1:8799/api/health'

sync-memory:
	# Memory bank is now a git repo: github.com/Manikanta-Reddy-Pasala/claude-memory
	# Laptop: commit + push; MS/NUC pull on their own cron.
	cd ~/.claude/memory && git add -A && \
	    (git diff --cached --quiet || git commit -m "memory sync $$(date -Iseconds)") && \
	    git push

reindex-memory:
	ssh $(NUC_HOST) 'cd ~/.claude/memory && git pull --ff-only && cd ~/AIForgeCrew && .venv/bin/python scripts/runtime/reindex-daily.py'

index-all:
	ssh $(SSH_HOST) 'cd ~/AIForgeCrew && bash scripts/runtime/embed-backfill.py --limit 5000'

kill-all:
	ssh $(SSH_HOST) 'for r in architect sr_developer developer fact_extract; do launchctl bootout gui/$$(id -u)/com.aiforge.tick-$$r 2>/dev/null || true; done; launchctl bootout gui/$$(id -u)/com.aiforge.api 2>/dev/null || true; echo stopped'

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ build dist *.egg-info
