.PHONY: help install test ui clean

help:
	@echo "Dev targets:"
	@echo "  install   .venv + uv pip install -e .[dev]"
	@echo "  test      pytest tests/python"
	@echo "  ui        vite build (web/dist)"
	@echo "  clean     remove caches + build artifacts"
	@echo ""
	@echo "Run the full stack with: docker compose up -d --build  (see QUICKSTART.md)"

install:
	uv venv .venv
	.venv/bin/uv pip install -e ".[dev]"

test:
	.venv/bin/pytest tests/python -v

ui:
	cd web && npm install && npm run build

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ build dist *.egg-info
