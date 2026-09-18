# uv's managed CPython comes from GitHub, not a package index: never download one.
export UV_PYTHON_DOWNLOADS ?= never

.PHONY: help install test test-docker ui vscode vscode-test clean aiforge

help:
	@echo "Dev targets:"
	@echo "  install   .venv + uv pip install -e .[dev]"
	@echo "  test      pytest tests/python"
	@echo "  test-docker  the CI run, in a throwaway container (fresh clone + uv.lock)"
	@echo "  ui        vite build (web/dist)"
	@echo "  vscode    the VS Code extension: packages/aiforge_vscode/dist/aiforge.vsix"
	@echo "  vscode-test  its unit tests + typecheck"
	@echo "  aiforge   the one installer: dist/cli/aiforge (./aiforge install = CLI + sandbox + web UI)"
	@echo "  clean     remove caches + build artifacts"
	@echo ""
	@echo "Run the full stack with: docker compose up -d --build  (see QUICKSTART.md)"

install:
	uv venv .venv
	# --all-extras, matching CI: tests require the declared extras (chonkie et
	# al) instead of skipping when they are missing, so a dev venv that lacks
	# them would go red on tests CI runs green.
	.venv/bin/uv pip install -e ".[dev,xlsx,structured,crawl,chunking,embed-static]"

test:
	.venv/bin/pytest tests/python -v

test-docker:
	# Fresh environment: clean clone of HEAD, uv sync --frozen, plain pytest.
	# Extra args: make test-docker ARGS="-m live_tmux"
	scripts/test_in_docker.sh $(ARGS)

# ── the one installer: the `aiforge` binary (installer/README.md) ─────
# It carries the sandbox source: `./aiforge install` = CLI + sandbox + web UI.
aiforge:
	installer/cli/build-binary.sh

ui:
	cd web && npm install && npm run build

# The VS Code extension (packages/aiforge_vscode/README.md): build + package.
vscode:
	cd packages/aiforge_vscode && npm ci && npm run package

vscode-test:
	cd packages/aiforge_vscode && npm ci && npm run typecheck && npm test

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ build dist *.egg-info
