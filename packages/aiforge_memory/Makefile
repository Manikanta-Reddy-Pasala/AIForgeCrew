.PHONY: help install test test-L1 test-L2 test-L3 test-L4 test-L5 test-L6 test-L7 \
        test-L8 test-L9 test-L10 test-L11 test-L12 test-L13 test-L14 test-L15 \
        test-unit doctor lint schedule

help:
	@echo "AiForgeMemory dev targets:"
	@echo "  install     uv venv + uv pip install -e .[dev]"
	@echo "  test        full pytest"
	@echo "  test-L<N>   per-layer gate (L1..L15)"
	@echo "  test-unit   only no-infrastructure tests"
	@echo "  doctor      check repomix + neo4j + llm sidecars"
	@echo "  lint        ruff check"
	@echo "  schedule    aiforge-memory schedule status"

install:
	uv venv .venv
	uv pip install -e ".[dev]"

test:
	.venv/bin/pytest aiforge_memory/tests -v

test-L1:
	.venv/bin/pytest aiforge_memory/tests/L1_repo_node/ -v

test-L2:
	.venv/bin/pytest aiforge_memory/tests/L2_service_extract/ -v

test-L3:
	.venv/bin/pytest aiforge_memory/tests/L3_file_summary/ -v

test-L4:
	.venv/bin/pytest aiforge_memory/tests/L4_symbols/ -v

test-L5:
	.venv/bin/pytest aiforge_memory/tests/L5_chunks_vectors/ -v

test-L6:
	.venv/bin/pytest aiforge_memory/tests/L6_translator/ -v

test-L7:
	.venv/bin/pytest aiforge_memory/tests/L7_bundle/ -v

test-L8:
	.venv/bin/pytest aiforge_memory/tests/L8_memory/ -v

test-L9:
	.venv/bin/pytest aiforge_memory/tests/L9_link/ -v

test-L10:
	.venv/bin/pytest aiforge_memory/tests/L10_delta/ -v

test-L11:
	.venv/bin/pytest aiforge_memory/tests/L11_eval/ -v

test-L12:
	.venv/bin/pytest aiforge_memory/tests/L12_scheduler/ -v

test-L13:
	.venv/bin/pytest aiforge_memory/tests/L13_symbol_enrich/ -v

test-L14:
	.venv/bin/pytest aiforge_memory/tests/L14_lsp/ -v

test-L15:
	.venv/bin/pytest aiforge_memory/tests/L15_git_meta/ -v

test-unit:
	.venv/bin/pytest aiforge_memory/tests/L8_memory/test_state_db_git.py \
	                 aiforge_memory/tests/L9_link/ \
	                 aiforge_memory/tests/L10_delta/ \
	                 aiforge_memory/tests/L11_eval/ \
	                 aiforge_memory/tests/L12_scheduler/ \
	                 aiforge_memory/tests/L13_symbol_enrich/ \
	                 aiforge_memory/tests/L14_lsp/ \
	                 aiforge_memory/tests/L15_git_meta/ -v

doctor:
	.venv/bin/aiforge-memory doctor

schedule:
	.venv/bin/aiforge-memory schedule status

lint:
	.venv/bin/ruff check aiforge_memory
