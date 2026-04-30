# Repomix Output for tiny_repo

## File: README.md

# tiny_repo — codemem L1 fixture

Toy repo used by Stage 1+2 ingest tests.

## Build / run

    make build
    make test
    python src/main.py

## Port-forward

    kubectl port-forward svc/tiny-repo 8080:8080 -n default

## File: Makefile

.PHONY: build test run

build:
	python -m compileall src

test:
	python -m pytest tests/ -v

run:
	python src/main.py

## File: src/main.py

"""Tiny demo service used by codemem L1 tests."""

def hello(name: str) -> str:
    return f"hello, {name}"


if __name__ == "__main__":
    print(hello("world"))
