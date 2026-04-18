.PHONY: setup lint test validate permission-check clean help \
        models download verify server load health bench bench-concurrent

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
	@echo ""
	@echo "P0 model targets (run on Mac Studio or via SSH_HOST):"
	@echo "  models             full pipeline: download + verify + server + load + health"
	@echo "  download           idempotent model download (reads security/model-checksums.yml)"
	@echo "  verify             sha256 verify all entries"
	@echo "  server             start LM Studio OpenAI-compat server on :1234"
	@echo "  load               load role models into memory (128K ctx default)"
	@echo "  health             probe /v1/models + per-role inference"
	@echo "  bench              P0 benchmark harness (solo per role)"
	@echo "  bench-concurrent   paired concurrent throughput bench"

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

# ---- P0 model pipeline (executed on Mac Studio or via SSH_HOST) ----
# All targets run remotely via `ssh $(SSH_HOST) 'bash -s' < scripts/X.sh`
# so the invocation works from any dev machine.

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

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache __pycache__ build dist *.egg-info
