.PHONY: setup lint test validate permission-check clean help \
        models download verify server load health bench bench-concurrent bench-passk \
        aiforge-install aiforge-test aiforge-doctor aiforge \
        mempalace-install mempalace-test \
        rag-install rag-reindex rag-query crg-query \
        paperclip-install paperclip-start paperclip-stop paperclip-status \
        paperclip-bootstrap paperclip-tunnel \
        hermes-install hermes-adapter-install \
        deploy-mac-studio hermes-configure hermes-skills-install \
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
	@echo "  hermes-install          NousResearch/hermes-agent CLI"
	@echo "  hermes-adapter-install  hermes-paperclip-adapter"

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

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache __pycache__ build dist *.egg-info
