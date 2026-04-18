.PHONY: setup lint test validate permission-check audit-tools clean help \
        models download verify server load health bench bench-concurrent bench-passk \
        aiforge-install aiforge-test aiforge-doctor aiforge \
        hermes-test hermes mempalace-install mempalace-test \
        rag-install rag-reindex rag-query crg-query \
        paperclip-install paperclip-start paperclip-stop paperclip-status

PY := python3
PIP := $(PY) -m pip

# SSH target for Mac Studio P0 work. Override: `make models SSH_HOST=user@host`.
SSH_HOST ?= manikanta@192.168.70.185

help:
	@echo "Dev targets:"
	@echo "  setup              install Python + Node + shell tooling"
	@echo "  lint               yamllint + markdownlint + shellcheck"
	@echo "  test               bats + pytest"
	@echo "  validate           JSON-schema validate configs"
	@echo "  permission-check   enforce DESIGN.md §5.2 permission matrix"
	@echo "  audit-tools        verify no network-capable tool handlers"
	@echo ""
	@echo "P0 model targets (run on Mac Studio or via SSH_HOST):"
	@echo "  models             full pipeline: download + verify + server + load + health"
	@echo "  download           idempotent model download"
	@echo "  verify             sha256 verify all entries"
	@echo "  server             start LM Studio OpenAI-compat server on :1234"
	@echo "  load               load role models into memory (128K ctx default)"
	@echo "  health             probe /v1/models + per-role inference"
	@echo "  bench              P0 benchmark harness (solo per role)"
	@echo "  bench-concurrent   paired concurrent throughput bench"
	@echo "  bench-passk        pass@1 harness on docs/eval/tickets/"
	@echo ""
	@echo "AIForge-core runtime (local Hermes-side orchestrator, macOS-only):"
	@echo "  aiforge-install    create .venv/ + uv pip install -e .[dev]"
	@echo "  aiforge-test       run pytest suite"
	@echo "  aiforge-doctor     sanity-check config + permissions + DB"
	@echo "  aiforge -- ARGS    run aiforge CLI (e.g. 'make aiforge -- ticket list')"
	@echo ""
	@echo "Hermes agent runtime:"
	@echo "  hermes-test        hermes tests"
	@echo "  hermes -- ARGS     hermes CLI"
	@echo ""
	@echo "Memory / RAG / CRG:"
	@echo "  mempalace-install  install MemPalace + init 5 palaces"
	@echo "  mempalace-test     mempalace tests"
	@echo "  rag-install        install chromadb + initial reindex"
	@echo "  rag-reindex        rebuild RAG index"
	@echo "  rag-query -- Q     RAG query"
	@echo "  crg-query -- PATH  code-review-graph blast radius"
	@echo ""
	@echo "Real Paperclip UI (Node, bring-your-own-agent dashboard):"
	@echo "  paperclip-install  npx paperclipai onboard on Mac Studio"
	@echo "  paperclip-start    start Paperclip server + UI on :3100"
	@echo "  paperclip-stop     stop Paperclip server"
	@echo "  paperclip-status   show Paperclip server status"

setup:
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	@command -v bats >/dev/null || echo "WARN: bats-core not installed (brew install bats-core)"
	@command -v shellcheck >/dev/null || echo "WARN: shellcheck not installed"
	@command -v yamllint >/dev/null || echo "WARN: yamllint not installed (pip install yamllint)"
	@command -v markdownlint-cli2 >/dev/null || echo "WARN: markdownlint-cli2 not installed (npm i -g markdownlint-cli2)"

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

audit-tools:
	.venv/bin/python tools/audit_tool_network.py

# ---- P0 model pipeline (executed on Mac Studio or via SSH_HOST) ----
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

# ---- AIForge-core runtime (local, macOS-only) ----
aiforge-install:
	bash scripts/install-aiforge.sh

aiforge-test:
	.venv/bin/pytest tests/python -v

aiforge-doctor:
	.venv/bin/aiforge doctor

aiforge:
	@.venv/bin/aiforge $(filter-out $@,$(MAKECMDGOALS))

# ---- Hermes agent runtime ----
hermes-test:
	.venv/bin/pytest tests/python/test_hermes_*.py -v

hermes:
	@.venv/bin/hermes $(filter-out $@,$(MAKECMDGOALS))

# ---- MemPalace two-tier memory ----
mempalace-install:
	bash scripts/install-mempalace.sh

mempalace-test:
	.venv/bin/pytest tests/python/test_paperclip_mem.py -v

# ---- RAG + code-review-graph ----
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

# ---- Real Paperclip UI (runs on Mac Studio) ----
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

paperclip-tunnel:
	@echo "Opening SSH tunnel: laptop:3100 → Mac Studio:3100"
	@echo "Then open http://localhost:3100 in your browser. Ctrl-C to close tunnel."
	ssh -L 3100:localhost:3100 -N $(SSH_HOST)

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache __pycache__ build dist *.egg-info
