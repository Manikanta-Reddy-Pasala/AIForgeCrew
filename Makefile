.PHONY: setup lint test validate permission-check clean help

PY := python3
PIP := $(PY) -m pip

help:
	@echo "Targets: setup lint test validate permission-check clean"

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

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache __pycache__ build dist *.egg-info
