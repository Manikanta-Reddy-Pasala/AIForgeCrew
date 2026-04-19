.PHONY: setup lint test validate permission-check clean help \
        models download verify server load health bench bench-concurrent bench-passk \
        aiforge-install aiforge-test aiforge-doctor aiforge \
        mempalace-install mempalace-test \
        rag-install rag-reindex rag-query crg-query \
        paperclip-install paperclip-start paperclip-stop paperclip-status \
        paperclip-bootstrap paperclip-tunnel \
        hermes-install hermes-adapter-install \
        deploy-mac-studio hermes-configure hermes-skills-install \
        hermes-skills-hub-install hermes-hindsight-setup hermes-memory-seed hermes-memory-stats \
        brew-install-macstudio reboot-macstudio \
        hermes-login hermes-dashboard-start hermes-dashboard-stop hermes-dashboard-tunnel \
        claude-cli-install sync-memory-push sync-memory-pull sync-code-repos mempalace-index-all

PY := python3
PIP := $(PY) -m pip

# SSH target for Mac Studio work. Override: `make X SSH_HOST=user@host`.
SSH_HOST ?= manikanta@192.168.70.185

help:
	@echo "Dev targets:"
	@echo "  setup              install Python + Node + shell tooling"
	@echo "  lint               yamllint + markdownlint + shellcheck"
	@echo "  test               bats + pytest"
	@echo "  validate           JSON-schema validate configs"
	@echo "  permission-check   enforce DESIGN.md §5.2 permission matrix"
	@echo ""
	@echo "P0 model targets (run on Mac Studio or via SSH_HOST):"
	@echo "  models             full pipeline: download + verify + server + load + health"
	@echo "  download           idempotent model download"
	@echo "  verify             sha256 verify all entries"
	@echo "  server             start LM Studio OpenAI-compat server on :1234"
	@echo "  load               load role models into memory (128K ctx default)"
	@echo "  health             probe /v1/models + per-role inference"
	@echo "  bench              solo benchmark harness"
	@echo "  bench-concurrent   paired concurrent throughput bench"
	@echo "  bench-passk        pass@1 harness on docs/eval/tickets/"
	@echo ""
	@echo "aiforge-core runtime (local, macOS-only):"
	@echo "  aiforge-install    .venv + uv pip install -e .[dev]"
	@echo "  aiforge-test       pytest suite"
	@echo "  aiforge-doctor     sanity-check config + permissions + DB"
	@echo "  aiforge -- ARGS    run aiforge CLI"
	@echo ""
	@echo "Memory / RAG / CRG:"
	@echo "  mempalace-install  install MemPalace + init 5 palaces"
	@echo "  mempalace-test     mempalace tests"
	@echo "  rag-install        install chromadb + initial reindex"
	@echo "  rag-reindex        rebuild RAG index"
	@echo "  rag-query -- Q     RAG query"
	@echo "  crg-query -- PATH  code-review-graph blast radius"
	@echo ""
	@echo "Real Paperclip UI (runs on Mac Studio):"
	@echo "  paperclip-install  npx paperclipai onboard"
	@echo "  paperclip-start    start server on :3100"
	@echo "  paperclip-stop     stop server"
	@echo "  paperclip-status   show server status"
	@echo "  paperclip-bootstrap create OneShell company + 4 agents"
	@echo "  paperclip-tunnel   ssh -L 3100 → laptop browser"
	@echo ""
	@echo "Real Hermes Agent (runs on Mac Studio):"
	@echo "  hermes-install              NousResearch/hermes-agent CLI"
	@echo "  hermes-adapter-install      hermes-paperclip-adapter"
	@echo "  hermes-skills-install       install aiforge_core skill pack (our DESIGN tooling)"
	@echo "  hermes-skills-hub-install   install official optional + community Hermes skills"
	@echo "  hermes-hindsight-setup      enable Hindsight memory provider (replaces MemPalace)"
	@echo "  hermes-memory-seed          import ~/.claude/memory into Hindsight"
	@echo "  hermes-memory-stats         show memory row count + sample recall"

setup:
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

lint:
	yamllint -c .yamllint.yml .
	markdownlint-cli2 "**/*.md" "#node_modules"
	shellcheck scripts/*.sh tests/shell/*.bats || true

test:
	bats tests/shell
	$(PY) -m pytest tests/python -v

validate:
	$(PY) tools/validate_configs.py

permission-check:
	$(PY) tools/check_permission_matrix.py

# ---- P0 model pipeline ----
models:
	ssh $(SSH_HOST) 'bash -s' < scripts/setup-models.sh

download:
	scp security/model-checksums.yml $(SSH_HOST):/tmp/aiforge-checksums.yml >/dev/null
	ssh $(SSH_HOST) "MANIFEST=/tmp/aiforge-checksums.yml bash -s" < scripts/download-models.sh

verify:
	scp security/model-checksums.yml $(SSH_HOST):/tmp/aiforge-checksums.yml >/dev/null
	ssh $(SSH_HOST) "MANIFEST=/tmp/aiforge-checksums.yml bash -s" < scripts/verify-checksums.sh

server:
	ssh $(SSH_HOST) 'bash -s' < scripts/start-servers.sh

load:
	scp security/model-checksums.yml $(SSH_HOST):/tmp/aiforge-checksums.yml >/dev/null
	ssh $(SSH_HOST) "MANIFEST=/tmp/aiforge-checksums.yml bash -s" < scripts/load-models.sh

health:
	ssh $(SSH_HOST) 'bash -s' < scripts/health-check.sh

bench:
	ssh $(SSH_HOST) 'bash -s' < scripts/benchmark-models.sh

bench-concurrent:
	ssh $(SSH_HOST) 'bash -s' < scripts/benchmark-concurrent.sh

bench-passk:
	scp -r docs/eval $(SSH_HOST):/tmp/aiforge-eval/ >/dev/null
	ssh $(SSH_HOST) "EVAL_DIR=/tmp/aiforge-eval/tickets bash -s" < scripts/benchmark-passk.sh

# ---- aiforge-core runtime (local) ----
aiforge-install:
	bash scripts/install-aiforge.sh

aiforge-test:
	.venv/bin/pytest tests/python -v

aiforge-doctor:
	.venv/bin/aiforge doctor

aiforge:
	@.venv/bin/aiforge $(filter-out $@,$(MAKECMDGOALS))

# ---- MemPalace ----
mempalace-install:
	ssh $(SSH_HOST) 'cd ~/AIForgeCrew && bash scripts/install-mempalace.sh'

mempalace-test:
	.venv/bin/pytest tests/python/test_paperclip_mem.py -v

# ---- RAG + CRG ----
rag-install:
	bash scripts/install-rag.sh

rag-reindex:
	.venv/bin/python -c "from pathlib import Path; from aiforge_core.rag import RagIndex; print(RagIndex(Path('.')).reindex())"

rag-query:
	@.venv/bin/python -c "import sys; from pathlib import Path; from aiforge_core.rag import RagIndex; \
	q=' '.join(sys.argv[1:]) or 'permission matrix'; \
	idx = RagIndex(Path('.')); \
	[print(f'[{h.source}]', h.text[:200].replace(chr(10), ' | ')[:200], '...') for h in idx.query(q)]" $(filter-out $@,$(MAKECMDGOALS))

crg-query:
	@.venv/bin/python -c "import sys; from pathlib import Path; from aiforge_core.crg import build_graph, blast_radius; \
	t=sys.argv[1] if len(sys.argv)>1 else 'aiforge_core/store.py'; \
	g=build_graph(Path('.')); \
	import json; print(json.dumps(blast_radius(g, t), indent=2))" $(filter-out $@,$(MAKECMDGOALS))

# ---- Real Paperclip UI (Mac Studio) ----
paperclip-install:
	ssh $(SSH_HOST) 'bash -s' < scripts/install-paperclip-ui.sh

paperclip-start:
	ssh $(SSH_HOST) 'bash -s' < scripts/paperclip-start.sh

paperclip-stop:
	ssh $(SSH_HOST) 'pkill -f paperclipai || true; echo stopped'

paperclip-status:
	ssh $(SSH_HOST) 'curl -s http://localhost:3100/api/health 2>&1 || echo not running'

paperclip-bootstrap:
	ssh $(SSH_HOST) 'bash -s' < scripts/paperclip-bootstrap-agents.sh

paperclip-install-agent-instructions:
	scp -rq agents/ $(SSH_HOST):$$HOME/AIForgeCrew-tmp-agents/
	ssh $(SSH_HOST) 'REPO=$$HOME/AIForgeCrew-tmp-agents bash -s' < scripts/paperclip-install-agent-instructions.sh
	ssh $(SSH_HOST) 'rm -rf $$HOME/AIForgeCrew-tmp-agents'

paperclip-em-use-claude:
	ssh $(SSH_HOST) 'bash -s' < scripts/paperclip-em-use-claude.sh

patch-hermes-adapter:
	ssh $(SSH_HOST) 'bash -s' < scripts/patch-hermes-adapter-for-lmstudio.sh

paperclip-tunnel:
	@echo "Opening SSH tunnel: laptop:3100 → Mac Studio:3100"
	@echo "Then open http://localhost:3100 in your browser. Ctrl-C closes tunnel."
	ssh -L 3100:localhost:3100 -N $(SSH_HOST)

# ---- Real Hermes Agent (Mac Studio) ----
hermes-install:
	ssh $(SSH_HOST) 'bash -s' < scripts/install-hermes-agent.sh

hermes-adapter-install:
	ssh $(SSH_HOST) 'bash -s' < scripts/install-hermes-adapter.sh

deploy-mac-studio:
	ssh $(SSH_HOST) 'bash -s' < scripts/deploy-to-mac-studio.sh

hermes-configure:
	ssh $(SSH_HOST) 'bash -s' < scripts/hermes-configure.sh

hermes-skills-install:
	ssh $(SSH_HOST) 'bash -s' < scripts/install-aiforge-skills.sh

# Skills from the Hermes hub (official optional + community).
hermes-skills-hub-install:
	ssh $(SSH_HOST) 'bash -s' < scripts/hermes-install-skills.sh

# Homebrew + openssl@3 + postgresql@16 on Mac Studio.
brew-install-macstudio:
	ssh $(SSH_HOST) 'bash -s' < scripts/install-brew-macstudio.sh

# Reboot Mac Studio (needs passwordless sudo).
reboot-macstudio:
	@echo "Rebooting Mac Studio — will reconnect after ~60s"
	ssh $(SSH_HOST) 'sudo shutdown -r now' || true
	@sleep 70
	@until ssh -o ConnectTimeout=5 $(SSH_HOST) 'uptime' 2>/dev/null; do sleep 5; done
	@echo "Mac Studio back online."

# Hindsight memory provider (replaces MemPalace + pgmem).
hermes-hindsight-setup:
	ssh -t $(SSH_HOST) 'bash -s' < scripts/hermes-setup-hindsight.sh

# Wire Hindsight into Claude Code CLI (EM uses claude_local adapter).
claude-mcp-hindsight:
	ssh $(SSH_HOST) 'bash -s' < scripts/claude-mcp-hindsight.sh

hermes-memory-seed:
	scp -q -r ~/.claude/memory $(SSH_HOST):/tmp/claude-memory-seed/ 2>/dev/null || true
	ssh $(SSH_HOST) 'CLAUDE_MEMORY=/tmp/claude-memory-seed bash -s' < scripts/hermes-seed-memory.sh

hermes-memory-stats:
	ssh $(SSH_HOST) 'export PATH=$$HOME/.local/bin:$$PATH; hermes memory stats 2>/dev/null || \
	  (export PATH=/opt/homebrew/opt/postgresql@16/bin:$$PATH; \
	   psql -d aiforge -c "SELECT COUNT(*) AS hindsight_rows FROM hindsight.memories" 2>&1 | head)'

hermes-login:
	ssh -t $(SSH_HOST) 'export PATH=$$HOME/.local/bin:$$PATH; hermes login --provider openai-codex'

hermes-dashboard-start:
	ssh $(SSH_HOST) 'bash -s' < scripts/hermes-dashboard-start.sh

hermes-dashboard-stop:
	ssh $(SSH_HOST) 'pkill -f "hermes dashboard" || true; echo stopped'

hermes-dashboard-tunnel:
	@echo "Opening SSH tunnel: laptop:9119 -> Mac Studio:9119"
	@echo "Then open http://localhost:9119 in your browser. Ctrl-C closes."
	ssh -L 9119:localhost:9119 -N $(SSH_HOST)

# ---- Claude CLI + memory + code repo sync on Mac Studio ----
claude-cli-install:
	ssh $(SSH_HOST) 'bash -s' < scripts/install-claude-cli-macstudio.sh

sync-memory-push:
	SSH_HOST=$(SSH_HOST) DIR=push bash scripts/sync-memory.sh

sync-memory-pull:
	SSH_HOST=$(SSH_HOST) DIR=pull bash scripts/sync-memory.sh

sync-code-repos:
	SSH_HOST=$(SSH_HOST) bash scripts/sync-code-repos.sh

mempalace-index-all:
	ssh $(SSH_HOST) 'bash -s' < scripts/mempalace-index-all.sh

mempalace-wipe-reindex:
	scp scripts/mempalace-index-all.sh $(SSH_HOST):/tmp/mempalace-index-all.sh >/dev/null
	scp scripts/mempalace-wipe-reindex.sh $(SSH_HOST):/tmp/mempalace-wipe-reindex.sh >/dev/null
	ssh $(SSH_HOST) 'chmod +x /tmp/mempalace-*.sh && bash /tmp/mempalace-wipe-reindex.sh'

mempalace-validate:
	ssh $(SSH_HOST) 'bash -s' < scripts/mempalace-validate.sh

# ---- pgvector-backed memory (supersedes MemPalace) ----
pgvector-install:
	ssh -t $(SSH_HOST) 'cd ~/AIForgeCrew && git fetch origin && git reset --hard origin/main && bash scripts/install-pgvector-macstudio.sh'

pgmem-import:
	ssh $(SSH_HOST) 'bash -s' < scripts/pgmem-import.sh

# ---- Model management (download + compute sha256 + delete unused) ----
models-delete-unused:
	scp -q security/model-checksums.yml $(SSH_HOST):/tmp/aiforge-checksums.yml >/dev/null
	ssh -t $(SSH_HOST) "MANIFEST=/tmp/aiforge-checksums.yml CONFIRM=$${CONFIRM:-0} bash -s" < scripts/delete-unused-models.sh

models-compute-sha:
	scp -q security/model-checksums.yml $(SSH_HOST):/tmp/aiforge-checksums.yml >/dev/null
	ssh $(SSH_HOST) "MANIFEST=/tmp/aiforge-checksums.yml bash -s" < scripts/compute-checksums.sh
	scp -q $(SSH_HOST):/tmp/aiforge-checksums.yml /tmp/aiforge-checksums.updated.yml
	@echo "Updated manifest on Mac Studio at /tmp/aiforge-checksums.yml"
	@echo "Review /tmp/aiforge-checksums.updated.yml and copy over security/model-checksums.yml if happy."

models-refresh: download
	@echo "Now run: make models-compute-sha && make models-delete-unused CONFIRM=1"

pgmem-validate:
	ssh $(SSH_HOST) 'export PATH=/opt/homebrew/opt/postgresql@16/bin:$$PATH; \
	  psql -d aiforge -c "SELECT wing, COUNT(*) FROM memories GROUP BY wing ORDER BY 2 DESC" && \
	  ~/AIForgeCrew/.venv/bin/python -c "from aiforge_core.pgmem import PgMemBus; \
	  hits = PgMemBus().search(\"sr-developer\", \"permission matrix DESIGN\", limit=3); \
	  [print(h[\"wing\"], h[\"source\"], round(h[\"score\"],3), h[\"text\"][:140]) for h in hits]"'

# ---- Auto-start all services on Mac Studio login ----
autostart-install:
	ssh $(SSH_HOST) 'bash -s' < scripts/autostart-install.sh

autostart-uninstall:
	ssh $(SSH_HOST) 'bash -s' < scripts/autostart-uninstall.sh

autostart-status:
	ssh $(SSH_HOST) 'launchctl list | grep com.aiforge; echo ---logs---; ls -la ~/aiforge-logs/*.log 2>/dev/null | head'

# ---- Caddy reverse proxy on Mac Studio + hostnames on laptop ----
caddy-install:
	ssh -t $(SSH_HOST) 'cd ~/AIForgeCrew && git fetch origin && git reset --hard origin/main && bash scripts/install-caddy-macstudio.sh'

hosts-install-laptop:
	bash scripts/install-hosts-laptop.sh

hosts-install: hosts-install-laptop caddy-install
	@echo
	@echo "Done. Open in browser:"
	@echo "  http://paperclip.local"
	@echo "  http://hermes.local"

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache __pycache__ build dist *.egg-info
