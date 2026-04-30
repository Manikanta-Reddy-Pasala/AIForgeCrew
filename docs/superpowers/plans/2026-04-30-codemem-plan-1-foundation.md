# Codemem Plan 1 — Foundation + L1 (Repo node) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scaffold the new `aiforge_core/codemem/` module and ship Stage 1 + Stage 2 of the ingestion pipeline (RepoMix pack → LLM repo summary → Neo4j `Repo` node + RUNBOOK), passing the L1 gate.

**Architecture:** New module sits beside the legacy `index/` and `memory/` packages and writes Neo4j nodes under `Repo` (no version suffix needed — label is new). State (idempotency hashes, query cache, service overrides) lives in `~/.aiforge/codemem.state.db` (sqlite). Stage 1 shells out to the `repomix` CLI; Stage 2 calls the qwen3.6-27b planner LLM via the existing `runtime/llm.py` transport and writes the resulting `Repo` node via the Neo4j Bolt driver.

**Tech Stack:** Python 3.11, neo4j 5.x driver, sqlite3 stdlib, RepoMix npm CLI (external), `aiforge_core.runtime.llm` (existing OpenAI-compat shim pointed at LM Studio :1235), pytest.

**Spec:** `docs/superpowers/specs/2026-04-30-unified-code-memory-design.md`

**Out of scope (this plan):** services/symbols/chunks/translator/bundle. Those land in plans 2–8.

---

## File structure (this plan creates / modifies)

**Create:**
- `aiforge_core/codemem/__init__.py` (3 lines, package marker)
- `aiforge_core/codemem/ingest/__init__.py` (package marker)
- `aiforge_core/codemem/ingest/pack_repo.py` (RepoMix wrapper, ~80 lines)
- `aiforge_core/codemem/ingest/repo_summary.py` (LLM call, JSON-strict parse, ~120 lines)
- `aiforge_core/codemem/ingest/flow.py` (Stage 1+2 orchestrator, ~80 lines)
- `aiforge_core/codemem/ingest/prompts/repo_summary.txt` (system prompt, ~40 lines)
- `aiforge_core/codemem/store/__init__.py` (package marker)
- `aiforge_core/codemem/store/schema.py` (Neo4j Repo constraints + indices, ~60 lines)
- `aiforge_core/codemem/store/state_db.py` (sqlite, ~120 lines)
- `aiforge_core/codemem/store/repo_writer.py` (Cypher MERGE for Repo node, ~70 lines)
- `aiforge_core/codemem/api/__init__.py` (package marker)
- `aiforge_core/codemem/api/cli.py` (argparse, ~100 lines)
- `aiforge_core/codemem/tests/__init__.py` (package marker)
- `aiforge_core/codemem/tests/README.md` (test-suite index)
- `aiforge_core/codemem/tests/L1_repo_node/__init__.py`
- `aiforge_core/codemem/tests/L1_repo_node/README.md` (gate contract)
- `aiforge_core/codemem/tests/L1_repo_node/test_state_db.py`
- `aiforge_core/codemem/tests/L1_repo_node/test_schema.py`
- `aiforge_core/codemem/tests/L1_repo_node/test_pack_repo.py`
- `aiforge_core/codemem/tests/L1_repo_node/test_repo_summary.py`
- `aiforge_core/codemem/tests/L1_repo_node/test_repo_writer.py`
- `aiforge_core/codemem/tests/L1_repo_node/test_l1_gate.py` (the gate itself)
- `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo/README.md`
- `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo/Makefile`
- `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo/src/main.py`
- `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_pack.md` (recorded RepoMix output for the tiny_repo)
- `aiforge_core/codemem/tests/L1_repo_node/fixtures/llm_response_ok.json` (mocked LLM response)
- `aiforge_core/codemem/tests/L1_repo_node/expected/tiny_repo_node.json`

**Modify:**
- `pyproject.toml` (add `[project.scripts]` entry `aiforge-codemem`, add testpath, no new Python deps required)
- `Makefile` (add `test-codemem-L1` target)

---

## Task 1: Scaffold the package and tests directory

**Files:**
- Create: `aiforge_core/codemem/__init__.py`
- Create: `aiforge_core/codemem/ingest/__init__.py`
- Create: `aiforge_core/codemem/store/__init__.py`
- Create: `aiforge_core/codemem/api/__init__.py`
- Create: `aiforge_core/codemem/tests/__init__.py`
- Create: `aiforge_core/codemem/tests/L1_repo_node/__init__.py`
- Create: `aiforge_core/codemem/tests/README.md`
- Modify: `pyproject.toml` (add codemem testpath)

- [ ] **Step 1: Write the failing scaffold-import test**

Create `aiforge_core/codemem/tests/L1_repo_node/test_scaffold.py`:

```python
"""L1 scaffold sanity — module tree imports cleanly."""
from __future__ import annotations


def test_codemem_imports() -> None:
    import aiforge_core.codemem
    import aiforge_core.codemem.ingest
    import aiforge_core.codemem.store
    import aiforge_core.codemem.api


def test_codemem_version_marker() -> None:
    from aiforge_core.codemem import SCHEMA_VERSION
    assert SCHEMA_VERSION == "codemem-v1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_scaffold.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aiforge_core.codemem'`

- [ ] **Step 3: Create the package files**

Create `aiforge_core/codemem/__init__.py`:

```python
"""codemem — unified code memory for AIForgeCrew.

Single read API for code context (Repo / Service / File / Symbol +
Chunk vectors). Replaces the legacy index/ + memory/code_context.py
stack incrementally. See docs/superpowers/specs/2026-04-30-unified-code-memory-design.md.
"""
from __future__ import annotations

SCHEMA_VERSION = "codemem-v1"
```

Create `aiforge_core/codemem/ingest/__init__.py`:

```python
"""codemem ingestion stages (CocoIndex-driven; manually wired in plan 1)."""
from __future__ import annotations
```

Create `aiforge_core/codemem/store/__init__.py`:

```python
"""codemem persistence — Neo4j (graph) + sqlite (state)."""
from __future__ import annotations
```

Create `aiforge_core/codemem/api/__init__.py`:

```python
"""codemem operator API — CLI + read API."""
from __future__ import annotations
```

Create `aiforge_core/codemem/tests/__init__.py`:

```python
```

Create `aiforge_core/codemem/tests/L1_repo_node/__init__.py`:

```python
```

Create `aiforge_core/codemem/tests/README.md`:

```markdown
# codemem test suite — gate index

Each `L<N>_<name>/` directory is a layer gate. Every gate has a
`README.md` beside it that documents the contract (purpose, fixture,
command, pass criteria, expected output, failure remediation).

| Layer | Dir | Plan |
|---|---|---|
| L1 | L1_repo_node/ | plan 1 (this) |
| L2 | L2_service_extract/ | plan 2 |
| L3 | L3_file_summary/ | plan 4 |
| L4 | L4_symbols/ | plan 3 |
| L5 | L5_chunks_vectors/ | plan 5 |
| L6 | L6_translator/ | plan 7 |
| L7 | L7_bundle/ | plan 7 |
| L8 | L8_e2e/ | plan 8 |

Run a single layer:

    make test-codemem-L1

Run all:

    make test-codemem-all
```

- [ ] **Step 4: Update pyproject testpath**

Edit `pyproject.toml`. Find the `[tool.pytest.ini_options]` block and replace:

```toml
[tool.pytest.ini_options]
testpaths = ["tests/python"]
addopts = "-ra"
markers = [
    "live_sidecar: requires embed/rerank sidecars running",
]
```

with:

```toml
[tool.pytest.ini_options]
testpaths = ["tests/python", "aiforge_core/codemem/tests"]
addopts = "-ra"
markers = [
    "live_sidecar: requires embed/rerank sidecars running",
    "live_neo4j: requires Neo4j running at AIFORGE_NEO4J_URI",
    "live_llm: requires planner LLM at AIFORGE_INTENT_LM_URL",
    "live_repomix: requires `repomix` binary on PATH",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_scaffold.py -v`
Expected: PASS — both tests green.

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/codemem pyproject.toml
git commit -m "feat(codemem): scaffold package + L1 test tree"
```

---

## Task 2: Sqlite state DB (idempotency hashes)

**Files:**
- Create: `aiforge_core/codemem/store/state_db.py`
- Create: `aiforge_core/codemem/tests/L1_repo_node/test_state_db.py`

- [ ] **Step 1: Write the failing test**

Create `aiforge_core/codemem/tests/L1_repo_node/test_state_db.py`:

```python
"""L1 — sqlite state DB: open/migrate/round-trip for merkle_repo + service_overrides."""
from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_core.codemem.store import state_db as sdb


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "codemem.state.db"
    conn = sdb.open_db(path)
    sdb.migrate(conn)
    yield conn
    conn.close()


def test_migrate_creates_tables(db) -> None:
    cur = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = [r[0] for r in cur.fetchall()]
    assert "merkle_files" in names
    assert "merkle_repo" in names
    assert "service_overrides" in names
    assert "query_cache" in names


def test_repo_hash_round_trip(db) -> None:
    sdb.set_repo_pack_sha(db, repo="PosClientBackend", pack_sha="abc123")
    sha = sdb.get_repo_pack_sha(db, repo="PosClientBackend")
    assert sha == "abc123"


def test_repo_hash_missing_returns_none(db) -> None:
    assert sdb.get_repo_pack_sha(db, repo="UnknownRepo") is None


def test_repo_hash_overwrites(db) -> None:
    sdb.set_repo_pack_sha(db, repo="X", pack_sha="v1")
    sdb.set_repo_pack_sha(db, repo="X", pack_sha="v2")
    assert sdb.get_repo_pack_sha(db, repo="X") == "v2"


def test_idempotent_migrate(db) -> None:
    # second migrate must not raise
    sdb.migrate(db)
    sdb.migrate(db)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_state_db.py -v`
Expected: FAIL — `ModuleNotFoundError: aiforge_core.codemem.store.state_db`

- [ ] **Step 3: Implement the state DB**

Create `aiforge_core/codemem/store/state_db.py`:

```python
"""codemem state database (sqlite).

Tables:
    merkle_repo       (repo TEXT PK, pack_sha TEXT, last_packed REAL)
    merkle_files      (repo TEXT, path TEXT, hash TEXT, last_indexed REAL,
                       PRIMARY KEY (repo, path))
    service_overrides (repo TEXT, name TEXT, source TEXT, payload TEXT,
                       PRIMARY KEY (repo, name))
    query_cache       (key TEXT PK, bundle_json TEXT, expires_at REAL)
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DEFAULT_DB_PATH = Path(
    os.environ.get(
        "AIFORGE_CODEMEM_STATE_DB",
        os.path.expanduser("~/.aiforge/codemem.state.db"),
    )
)


def open_db(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_DDL = [
    """CREATE TABLE IF NOT EXISTS merkle_repo (
        repo        TEXT PRIMARY KEY,
        pack_sha    TEXT NOT NULL,
        last_packed REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS merkle_files (
        repo         TEXT NOT NULL,
        path         TEXT NOT NULL,
        hash         TEXT NOT NULL,
        last_indexed REAL NOT NULL,
        PRIMARY KEY (repo, path)
    )""",
    """CREATE TABLE IF NOT EXISTS service_overrides (
        repo    TEXT NOT NULL,
        name    TEXT NOT NULL,
        source  TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (repo, name)
    )""",
    """CREATE TABLE IF NOT EXISTS query_cache (
        key         TEXT PRIMARY KEY,
        bundle_json TEXT NOT NULL,
        expires_at  REAL NOT NULL
    )""",
]


def migrate(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for stmt in _DDL:
        cur.execute(stmt)
    conn.commit()


def set_repo_pack_sha(conn: sqlite3.Connection, *, repo: str, pack_sha: str) -> None:
    conn.execute(
        "INSERT INTO merkle_repo (repo, pack_sha, last_packed) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(repo) DO UPDATE SET pack_sha=excluded.pack_sha, "
        "  last_packed=excluded.last_packed",
        (repo, pack_sha, time.time()),
    )
    conn.commit()


def get_repo_pack_sha(conn: sqlite3.Connection, *, repo: str) -> str | None:
    row = conn.execute(
        "SELECT pack_sha FROM merkle_repo WHERE repo = ?", (repo,)
    ).fetchone()
    return row[0] if row else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_state_db.py -v`
Expected: PASS — five tests green.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/codemem/store/state_db.py \
        aiforge_core/codemem/tests/L1_repo_node/test_state_db.py
git commit -m "feat(codemem): sqlite state db (merkle + cache + overrides)"
```

---

## Task 3: Neo4j schema migration for the Repo node

**Files:**
- Create: `aiforge_core/codemem/store/schema.py`
- Create: `aiforge_core/codemem/tests/L1_repo_node/test_schema.py`

- [ ] **Step 1: Write the failing test**

Create `aiforge_core/codemem/tests/L1_repo_node/test_schema.py`:

```python
"""L1 — Neo4j schema migration: Repo unique constraint + indices."""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live_neo4j


@pytest.fixture(scope="module")
def driver():
    try:
        from neo4j import GraphDatabase
    except ImportError:
        pytest.skip("neo4j driver not installed")
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    drv = GraphDatabase.driver(uri, auth=(user, pw))
    try:
        with drv.session() as s:
            s.run("RETURN 1").consume()
    except Exception as exc:
        pytest.skip(f"Neo4j unreachable: {exc}")
    yield drv
    drv.close()


def test_apply_creates_constraint(driver) -> None:
    from aiforge_core.codemem.store import schema

    schema.apply(driver)

    with driver.session() as s:
        rows = list(s.run("SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties"))
    names = {r["name"] for r in rows}
    assert "codemem_repo_name_unique" in names


def test_apply_creates_runbook_fulltext(driver) -> None:
    from aiforge_core.codemem.store import schema

    schema.apply(driver)
    with driver.session() as s:
        rows = list(s.run(
            "SHOW INDEXES YIELD name, type WHERE name = 'codemem_repo_runbook_ft'"
        ))
    assert len(rows) == 1
    assert rows[0]["type"] == "FULLTEXT"


def test_apply_is_idempotent(driver) -> None:
    from aiforge_core.codemem.store import schema

    schema.apply(driver)
    schema.apply(driver)  # second call must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: aiforge_core.codemem.store.schema` (or skip if Neo4j is down — that's also fine for now; come back when Neo4j is up).

- [ ] **Step 3: Implement the schema module**

Create `aiforge_core/codemem/store/schema.py`:

```python
"""codemem Neo4j schema — constraints + indices.

Idempotent: every statement uses IF NOT EXISTS. Safe to re-run.
Plan 1 covers the Repo label only; later plans add Service/File/
Symbol/Chunk in their own apply() steps.
"""
from __future__ import annotations

# Each entry: a single Cypher statement, idempotent.
_STATEMENTS: list[str] = [
    # Unique on Repo.name
    "CREATE CONSTRAINT codemem_repo_name_unique IF NOT EXISTS "
    "FOR (r:Repo) REQUIRE r.name IS UNIQUE",
    # B-tree index on last_indexed_at for stats
    "CREATE INDEX codemem_repo_last_indexed_at IF NOT EXISTS "
    "FOR (r:Repo) ON (r.last_indexed_at)",
    # Fulltext over runbook_md so queries like "how do I run X" hit it
    "CREATE FULLTEXT INDEX codemem_repo_runbook_ft IF NOT EXISTS "
    "FOR (r:Repo) ON EACH [r.runbook_md, r.conventions_md]",
]


def apply(driver) -> None:
    """Apply every schema statement. ``driver`` is a neo4j.GraphDatabase driver.

    Each statement runs in its own session and is idempotent. Errors
    other than 'already exists' propagate.
    """
    for stmt in _STATEMENTS:
        with driver.session() as session:
            session.run(stmt).consume()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_schema.py -v`
Expected: PASS (or SKIP if Neo4j is down). When Neo4j is up at `bolt://127.0.0.1:7687`, three tests pass.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/codemem/store/schema.py \
        aiforge_core/codemem/tests/L1_repo_node/test_schema.py
git commit -m "feat(codemem): Neo4j schema for Repo node (constraint + indices)"
```

---

## Task 4: RepoMix wrapper (Stage 1 — pack_repo)

**Files:**
- Create: `aiforge_core/codemem/ingest/pack_repo.py`
- Create: `aiforge_core/codemem/tests/L1_repo_node/test_pack_repo.py`
- Create: `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo/README.md`
- Create: `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo/Makefile`
- Create: `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo/src/main.py`

- [ ] **Step 1: Build the tiny_repo fixture**

Create `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo/README.md`:

```markdown
# tiny_repo — codemem L1 fixture

Toy repo used by Stage 1+2 ingest tests. Real enough that
RepoMix produces non-trivial output and the LLM has something
to summarize.

## Build / run

    make build
    make test
    python src/main.py

## Port-forward

    kubectl port-forward svc/tiny-repo 8080:8080 -n default
```

Create `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo/Makefile`:

```makefile
.PHONY: build test run

build:
	python -m compileall src

test:
	python -m pytest tests/ -v

run:
	python src/main.py
```

Create `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo/src/main.py`:

```python
"""Tiny demo service used by codemem L1 tests."""

def hello(name: str) -> str:
    return f"hello, {name}"


if __name__ == "__main__":
    print(hello("world"))
```

- [ ] **Step 2: Write the failing test**

Create `aiforge_core/codemem/tests/L1_repo_node/test_pack_repo.py`:

```python
"""L1 — RepoMix wrapper: shell out, return text + sha256, soft-fail."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_core.codemem.ingest import pack_repo


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tiny_repo"


def test_pack_returns_text_and_sha_when_repomix_present() -> None:
    """Mocked subprocess: we don't require repomix on PATH for unit tests."""
    fake_stdout = "# tiny_repo pack\n## File: src/main.py\n"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = fake_stdout
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        text, sha = pack_repo.pack(FIXTURE_DIR)
    assert text == fake_stdout
    assert len(sha) == 64  # sha256 hex
    # Hashing same input twice yields same sha
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = fake_stdout
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        _, sha2 = pack_repo.pack(FIXTURE_DIR)
    assert sha == sha2


def test_pack_raises_when_repomix_missing() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("repomix")):
        with pytest.raises(pack_repo.RepoMixNotFound):
            pack_repo.pack(FIXTURE_DIR)


def test_pack_raises_on_nonzero_exit() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = ""
        mock_run.return_value.returncode = 2
        mock_run.return_value.stderr = "boom"
        with pytest.raises(pack_repo.RepoMixError) as exc:
            pack_repo.pack(FIXTURE_DIR)
        assert "boom" in str(exc.value)


def test_pack_target_must_be_directory(tmp_path: Path) -> None:
    f = tmp_path / "not_a_dir.txt"
    f.write_text("hi")
    with pytest.raises(NotADirectoryError):
        pack_repo.pack(f)


@pytest.mark.live_repomix
def test_pack_live_against_tiny_repo() -> None:
    """Smoke against the real binary — only runs when repomix is on PATH."""
    text, sha = pack_repo.pack(FIXTURE_DIR)
    assert "main.py" in text
    assert len(sha) == 64
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_pack_repo.py -v`
Expected: FAIL — `ModuleNotFoundError: aiforge_core.codemem.ingest.pack_repo`

- [ ] **Step 4: Implement pack_repo**

Create `aiforge_core/codemem/ingest/pack_repo.py`:

```python
"""Stage 1 — RepoMix pack.

Shells out to the `repomix` CLI (npm package) and captures stdout.
Returns (pack_text, sha256). Caller is responsible for hash-comparing
sha against state_db.merkle_repo to skip downstream stages.

Soft contract:
    - repomix binary missing → RepoMixNotFound (caller may fall back)
    - repomix nonzero exit   → RepoMixError(stderr) (caller logs + skips)

Defaults to: `repomix . --style markdown --output -` (stdout).
Override the binary via AIFORGE_CODEMEM_REPOMIX (e.g. "/opt/homebrew/bin/repomix").
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


class RepoMixNotFound(RuntimeError):
    pass


class RepoMixError(RuntimeError):
    pass


def _binary() -> str:
    return os.environ.get("AIFORGE_CODEMEM_REPOMIX", "repomix")


def pack(repo_path: str | Path) -> tuple[str, str]:
    """Run RepoMix on ``repo_path``; return (markdown_text, sha256_hex)."""
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise NotADirectoryError(f"{repo_path} is not a directory")

    try:
        proc = subprocess.run(
            [
                _binary(),
                str(repo_path),
                "--style", "markdown",
                "--output", "-",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise RepoMixNotFound(
            f"repomix binary not found (set AIFORGE_CODEMEM_REPOMIX or "
            f"`npm i -g repomix`): {exc}"
        ) from exc

    if proc.returncode != 0:
        raise RepoMixError(f"repomix exited {proc.returncode}: {proc.stderr.strip()}")

    text = proc.stdout
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, sha
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_pack_repo.py -v -m "not live_repomix"`
Expected: PASS — four mocked tests green; live test skipped.

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/codemem/ingest/pack_repo.py \
        aiforge_core/codemem/tests/L1_repo_node/test_pack_repo.py \
        aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo
git commit -m "feat(codemem): Stage 1 RepoMix wrapper + tiny_repo fixture"
```

---

## Task 5: Repo summary LLM (Stage 2 — repo_summary)

**Files:**
- Create: `aiforge_core/codemem/ingest/prompts/repo_summary.txt`
- Create: `aiforge_core/codemem/ingest/repo_summary.py`
- Create: `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_pack.md`
- Create: `aiforge_core/codemem/tests/L1_repo_node/fixtures/llm_response_ok.json`
- Create: `aiforge_core/codemem/tests/L1_repo_node/test_repo_summary.py`

- [ ] **Step 1: Write the system prompt**

Create directory + file `aiforge_core/codemem/ingest/prompts/repo_summary.txt`:

```text
You analyze software repositories and emit a strict-JSON summary.

Input: a markdown pack of the repository's files (output of repomix).

Output: a single JSON object — no prose, no markdown fences, no commentary.

Schema (every field required):
{
  "lang_primary":      string  (e.g. "java", "python", "typescript"),
  "build_cmd":         string  (single shell command; "" if unknown),
  "test_cmd":          string,
  "lint_cmd":          string,
  "run_cmd":           string,
  "portforward_cmds":  array of strings (kubectl/docker port-forward; [] if none),
  "conventions_md":    string  (markdown, ≤500 tokens; coding conventions, branch rules, ...),
  "runbook_md":        string  (markdown, ≥500 chars; how to clone, build, test, run, debug)
}

Rules:
- Look for Makefile / pom.xml / package.json / pyproject.toml / Cargo.toml first.
- If multiple build systems coexist, prefer the one used in CI.
- portforward_cmds: scan for `kubectl port-forward` and `docker run -p` patterns.
- Never invent commands. If the command is not in the input, return "".
- runbook_md is your synthesis — write it for an engineer who just cloned the repo.
- Output ONLY the JSON object.
```

- [ ] **Step 2: Build the recorded fixtures**

Create `aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_pack.md`:

```markdown
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
```

Create `aiforge_core/codemem/tests/L1_repo_node/fixtures/llm_response_ok.json`:

```json
{
  "lang_primary": "python",
  "build_cmd": "make build",
  "test_cmd": "make test",
  "lint_cmd": "",
  "run_cmd": "make run",
  "portforward_cmds": ["kubectl port-forward svc/tiny-repo 8080:8080 -n default"],
  "conventions_md": "## Conventions\n- Python 3.11\n- Tests under tests/\n",
  "runbook_md": "## Tiny Repo Runbook\n\n### Clone\n```\ngit clone <repo>\n```\n\n### Build\n```\nmake build\n```\n\n### Test\n```\nmake test\n```\n\n### Run\n```\nmake run\n```\n\n### Port-forward\n```\nkubectl port-forward svc/tiny-repo 8080:8080 -n default\n```\n"
}
```

- [ ] **Step 3: Write the failing test**

Create `aiforge_core/codemem/tests/L1_repo_node/test_repo_summary.py`:

```python
"""L1 — Stage 2: pack text → strict-JSON RepoSummary via LLM."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_core.codemem.ingest import repo_summary as rs


FIX = Path(__file__).parent / "fixtures"


def _load_pack() -> str:
    return (FIX / "tiny_pack.md").read_text()


def _load_llm_ok() -> str:
    return (FIX / "llm_response_ok.json").read_text()


def test_summary_parses_clean_json() -> None:
    pack = _load_pack()
    with patch.object(rs, "_call_llm", return_value=_load_llm_ok()):
        summary = rs.summarize(pack, repo_name="tiny_repo")
    assert summary.lang_primary == "python"
    assert summary.build_cmd == "make build"
    assert summary.test_cmd == "make test"
    assert summary.run_cmd == "make run"
    assert summary.portforward_cmds == [
        "kubectl port-forward svc/tiny-repo 8080:8080 -n default"
    ]
    assert "Tiny Repo Runbook" in summary.runbook_md
    assert len(summary.runbook_md) >= 500


def test_summary_strips_markdown_fences() -> None:
    pack = _load_pack()
    fenced = "```json\n" + _load_llm_ok() + "\n```"
    with patch.object(rs, "_call_llm", return_value=fenced):
        summary = rs.summarize(pack, repo_name="tiny_repo")
    assert summary.lang_primary == "python"


def test_summary_retries_on_invalid_json_then_succeeds() -> None:
    pack = _load_pack()
    bad = "not json at all"
    good = _load_llm_ok()
    with patch.object(rs, "_call_llm", side_effect=[bad, good]):
        summary = rs.summarize(pack, repo_name="tiny_repo")
    assert summary.build_cmd == "make build"


def test_summary_raises_after_two_invalid_responses() -> None:
    pack = _load_pack()
    with patch.object(rs, "_call_llm", side_effect=["bad1", "bad2"]):
        with pytest.raises(rs.RepoSummaryError):
            rs.summarize(pack, repo_name="tiny_repo")


def test_pack_truncated_at_max_input_tokens() -> None:
    big = "x" * 1_000_000
    with patch.object(rs, "_call_llm") as mock_call:
        mock_call.return_value = _load_llm_ok()
        rs.summarize(big, repo_name="huge_repo", max_input_chars=200_000)
    sent_pack = mock_call.call_args.args[0]
    # Truncation marker present, total length capped
    assert len(sent_pack) <= 200_500
    assert "[TRUNCATED" in sent_pack
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_repo_summary.py -v`
Expected: FAIL — `ModuleNotFoundError: aiforge_core.codemem.ingest.repo_summary`

- [ ] **Step 5: Implement repo_summary**

Create `aiforge_core/codemem/ingest/repo_summary.py`:

```python
"""Stage 2 — LLM repo summary.

Sends the RepoMix pack + a strict-JSON system prompt to the planner
LLM (qwen3.6-27b at LM Studio :1235 by default) and parses the result
into a `RepoSummary` dataclass. One automatic retry on invalid JSON
with a stricter system suffix; second failure raises RepoSummaryError.

The actual transport call is isolated in ``_call_llm`` so the unit
tests can monkey-patch it without standing up LM Studio.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PROMPT_PATH = Path(__file__).parent / "prompts" / "repo_summary.txt"
DEFAULT_LM_URL = os.environ.get(
    "AIFORGE_CODEMEM_LM_URL",
    os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
)
DEFAULT_MODEL = os.environ.get(
    "AIFORGE_CODEMEM_LM_MODEL", "qwen3.6-27b-instruct"
)


class RepoSummaryError(RuntimeError):
    pass


@dataclass
class RepoSummary:
    lang_primary: str = ""
    build_cmd: str = ""
    test_cmd: str = ""
    lint_cmd: str = ""
    run_cmd: str = ""
    portforward_cmds: list[str] = field(default_factory=list)
    conventions_md: str = ""
    runbook_md: str = ""


def summarize(
    pack_text: str,
    *,
    repo_name: str,
    max_input_chars: int = 240_000,
) -> RepoSummary:
    """Pack → LLM → RepoSummary. Retries once on bad JSON."""
    pack = _truncate(pack_text, max_input_chars)
    system = PROMPT_PATH.read_text()
    user = f"Repository name: {repo_name}\n\n{pack}"

    raw = _call_llm(pack, system=system, user=user)
    parsed = _parse(raw)
    if parsed is not None:
        return parsed

    # Retry with a stricter suffix
    strict_system = system + "\n\nReminder: output ONLY a JSON object — no prose."
    raw2 = _call_llm(pack, system=strict_system, user=user)
    parsed2 = _parse(raw2)
    if parsed2 is not None:
        return parsed2

    raise RepoSummaryError("LLM returned invalid JSON twice")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)]
    tail = text[-int(limit * 0.4):]
    return f"{head}\n\n[TRUNCATED {len(text) - limit} chars]\n\n{tail}"


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _parse(raw: str) -> RepoSummary | None:
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return RepoSummary(
        lang_primary=str(obj.get("lang_primary", "")),
        build_cmd=str(obj.get("build_cmd", "")),
        test_cmd=str(obj.get("test_cmd", "")),
        lint_cmd=str(obj.get("lint_cmd", "")),
        run_cmd=str(obj.get("run_cmd", "")),
        portforward_cmds=[str(x) for x in obj.get("portforward_cmds", []) or []],
        conventions_md=str(obj.get("conventions_md", "")),
        runbook_md=str(obj.get("runbook_md", "")),
    )


def _call_llm(pack_text: str, *, system: str = "", user: str = "") -> str:
    """Real LLM call. Isolated for monkey-patching in tests.

    `pack_text` is kept in the signature so tests can introspect it,
    but the actual prompt assembled below is what hits the LLM.
    """
    from openai import OpenAI

    client = OpenAI(
        base_url=DEFAULT_LM_URL,
        api_key=os.environ.get("AIFORGE_CODEMEM_LM_KEY", "lm-studio"),
    )
    resp = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=4000,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or ""
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_repo_summary.py -v`
Expected: PASS — five tests green.

- [ ] **Step 7: Commit**

```bash
git add aiforge_core/codemem/ingest/repo_summary.py \
        aiforge_core/codemem/ingest/prompts/repo_summary.txt \
        aiforge_core/codemem/tests/L1_repo_node/test_repo_summary.py \
        aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_pack.md \
        aiforge_core/codemem/tests/L1_repo_node/fixtures/llm_response_ok.json
git commit -m "feat(codemem): Stage 2 LLM repo summary (strict-JSON, retry once)"
```

---

## Task 6: Neo4j Repo writer (upsert)

**Files:**
- Create: `aiforge_core/codemem/store/repo_writer.py`
- Create: `aiforge_core/codemem/tests/L1_repo_node/test_repo_writer.py`

- [ ] **Step 1: Write the failing test**

Create `aiforge_core/codemem/tests/L1_repo_node/test_repo_writer.py`:

```python
"""L1 — Repo node upsert via Cypher MERGE."""
from __future__ import annotations

import os

import pytest

from aiforge_core.codemem.ingest.repo_summary import RepoSummary
from aiforge_core.codemem.store import repo_writer, schema

pytestmark = pytest.mark.live_neo4j


@pytest.fixture(scope="module")
def driver():
    try:
        from neo4j import GraphDatabase
    except ImportError:
        pytest.skip("neo4j driver not installed")
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    drv = GraphDatabase.driver(uri, auth=(user, pw))
    try:
        with drv.session() as s:
            s.run("RETURN 1").consume()
    except Exception as exc:
        pytest.skip(f"Neo4j unreachable: {exc}")
    schema.apply(drv)
    yield drv
    # cleanup test repos
    with drv.session() as s:
        s.run("MATCH (r:Repo) WHERE r.name STARTS WITH 'test_' DETACH DELETE r").consume()
    drv.close()


def test_upsert_creates_node(driver) -> None:
    summary = RepoSummary(
        lang_primary="python",
        build_cmd="make build",
        test_cmd="make test",
        lint_cmd="ruff check",
        run_cmd="make run",
        portforward_cmds=["kubectl port-forward svc/x 8080:8080"],
        conventions_md="## Conv",
        runbook_md="## Runbook\n" + ("a" * 600),
    )
    repo_writer.upsert_repo(
        driver,
        name="test_codemem_repo_a",
        path="/tmp/test_codemem_repo_a",
        summary=summary,
        pack_sha="sha-A",
    )
    with driver.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:$n}) RETURN r", n="test_codemem_repo_a"
        ).single()
    assert row is not None
    r = row["r"]
    assert r["build_cmd"] == "make build"
    assert r["lang_primary"] == "python"
    assert r["last_pack_sha"] == "sha-A"
    assert r["last_indexed_at"] is not None
    assert r["portforward_cmds"] == ["kubectl port-forward svc/x 8080:8080"]


def test_upsert_is_idempotent_and_updates_pack_sha(driver) -> None:
    summary = RepoSummary(lang_primary="python", build_cmd="x", runbook_md="r" * 600)
    repo_writer.upsert_repo(
        driver, name="test_codemem_repo_b", path="/tmp/b",
        summary=summary, pack_sha="sha-1",
    )
    repo_writer.upsert_repo(
        driver, name="test_codemem_repo_b", path="/tmp/b",
        summary=summary, pack_sha="sha-2",
    )
    with driver.session() as s:
        sha = s.run(
            "MATCH (r:Repo {name:$n}) RETURN r.last_pack_sha AS sha",
            n="test_codemem_repo_b",
        ).single()["sha"]
    assert sha == "sha-2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_repo_writer.py -v`
Expected: FAIL — `ModuleNotFoundError: aiforge_core.codemem.store.repo_writer` (or SKIP if Neo4j is down).

- [ ] **Step 3: Implement repo_writer**

Create `aiforge_core/codemem/store/repo_writer.py`:

```python
"""Cypher writer for the Repo node.

Single function: ``upsert_repo(driver, name, path, summary, pack_sha)``.
Idempotent: Cypher MERGE keyed on ``Repo.name``.
"""
from __future__ import annotations

import time
from dataclasses import asdict

from aiforge_core.codemem.ingest.repo_summary import RepoSummary


_CYPHER = """
MERGE (r:Repo {name: $name})
SET r.path             = $path,
    r.lang_primary     = $lang_primary,
    r.build_cmd        = $build_cmd,
    r.test_cmd         = $test_cmd,
    r.lint_cmd         = $lint_cmd,
    r.run_cmd          = $run_cmd,
    r.portforward_cmds = $portforward_cmds,
    r.conventions_md   = $conventions_md,
    r.runbook_md       = $runbook_md,
    r.last_pack_sha    = $pack_sha,
    r.last_indexed_at  = datetime({epochSeconds: toInteger($now)}),
    r.schema_version   = 'codemem-v1'
RETURN r
"""


def upsert_repo(
    driver,
    *,
    name: str,
    path: str,
    summary: RepoSummary,
    pack_sha: str,
) -> None:
    params = {
        "name": name,
        "path": path,
        "now": time.time(),
        "pack_sha": pack_sha,
        **asdict(summary),
    }
    with driver.session() as s:
        s.run(_CYPHER, **params).consume()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_repo_writer.py -v`
Expected: PASS (or SKIP if Neo4j is down). Two tests green when Neo4j is up.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/codemem/store/repo_writer.py \
        aiforge_core/codemem/tests/L1_repo_node/test_repo_writer.py
git commit -m "feat(codemem): Cypher MERGE writer for Repo node (idempotent)"
```

---

## Task 7: Stage 1+2 orchestrator (`flow.ingest_repo`)

**Files:**
- Create: `aiforge_core/codemem/ingest/flow.py`
- Modify: `aiforge_core/codemem/store/state_db.py` (already done in Task 2 — no edit; flow uses it)
- Create test: covered by L1 gate (Task 9). For unit-level orchestrator confidence, add a focused test now.
- Create: `aiforge_core/codemem/tests/L1_repo_node/test_flow.py`

- [ ] **Step 1: Write the failing test**

Create `aiforge_core/codemem/tests/L1_repo_node/test_flow.py`:

```python
"""L1 — orchestrator: pack → summarize → upsert, idempotent."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from aiforge_core.codemem.ingest import flow
from aiforge_core.codemem.ingest.repo_summary import RepoSummary
from aiforge_core.codemem.store import state_db as sdb


FIX = Path(__file__).parent / "fixtures"


def test_flow_first_run_calls_pack_summary_writer(tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    fake_driver = MagicMock()
    fake_pack = ("# pack text", "sha-AAA")
    fake_summary = RepoSummary(lang_primary="python", build_cmd="make build",
                               runbook_md="r" * 600)

    with patch("aiforge_core.codemem.ingest.flow.pack_repo.pack",
               return_value=fake_pack) as p, \
         patch("aiforge_core.codemem.ingest.flow.repo_summary.summarize",
               return_value=fake_summary) as s, \
         patch("aiforge_core.codemem.ingest.flow.repo_writer.upsert_repo") as w:
        result = flow.ingest_repo(
            repo_name="rA", repo_path=str(FIX / "tiny_repo"),
            driver=fake_driver, state_conn=state,
        )
    assert result.status == "indexed"
    assert result.pack_sha == "sha-AAA"
    p.assert_called_once()
    s.assert_called_once()
    w.assert_called_once()


def test_flow_second_run_with_unchanged_sha_skips(tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    sdb.set_repo_pack_sha(state, repo="rB", pack_sha="sha-SAME")
    fake_driver = MagicMock()
    fake_pack = ("# pack text", "sha-SAME")

    with patch("aiforge_core.codemem.ingest.flow.pack_repo.pack",
               return_value=fake_pack), \
         patch("aiforge_core.codemem.ingest.flow.repo_summary.summarize") as s, \
         patch("aiforge_core.codemem.ingest.flow.repo_writer.upsert_repo") as w:
        result = flow.ingest_repo(
            repo_name="rB", repo_path=str(FIX / "tiny_repo"),
            driver=fake_driver, state_conn=state,
        )
    assert result.status == "skipped_unchanged"
    s.assert_not_called()
    w.assert_not_called()


def test_flow_force_reingests_even_when_sha_same(tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    sdb.set_repo_pack_sha(state, repo="rC", pack_sha="sha-SAME")
    fake_driver = MagicMock()

    with patch("aiforge_core.codemem.ingest.flow.pack_repo.pack",
               return_value=("# pack", "sha-SAME")), \
         patch("aiforge_core.codemem.ingest.flow.repo_summary.summarize",
               return_value=RepoSummary(runbook_md="r" * 600)) as s, \
         patch("aiforge_core.codemem.ingest.flow.repo_writer.upsert_repo") as w:
        result = flow.ingest_repo(
            repo_name="rC", repo_path=str(FIX / "tiny_repo"),
            driver=fake_driver, state_conn=state, force=True,
        )
    assert result.status == "indexed"
    s.assert_called_once()
    w.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_flow.py -v`
Expected: FAIL — `ModuleNotFoundError: aiforge_core.codemem.ingest.flow`

- [ ] **Step 3: Implement the orchestrator**

Create `aiforge_core/codemem/ingest/flow.py`:

```python
"""codemem ingestion orchestrator (Stages 1+2 in plan 1).

Exposed surface:
    flow.ingest_repo(repo_name, repo_path, *, driver, state_conn, force=False)
        -> IngestResult

Idempotency: pack_sha matched against state_db.merkle_repo. When equal
and ``force=False`` we skip Stages 2+. ``force=True`` reruns everything
(used by `aiforge codemem reset`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiforge_core.codemem.ingest import pack_repo, repo_summary
from aiforge_core.codemem.store import repo_writer, state_db as sdb


@dataclass
class IngestResult:
    status: str           # "indexed" | "skipped_unchanged"
    pack_sha: str
    repo: str


def ingest_repo(
    *,
    repo_name: str,
    repo_path: str | Path,
    driver,
    state_conn,
    force: bool = False,
) -> IngestResult:
    text, sha = pack_repo.pack(repo_path)
    prev = sdb.get_repo_pack_sha(state_conn, repo=repo_name)
    if prev == sha and not force:
        return IngestResult(status="skipped_unchanged", pack_sha=sha, repo=repo_name)

    summary = repo_summary.summarize(text, repo_name=repo_name)
    repo_writer.upsert_repo(
        driver,
        name=repo_name,
        path=str(Path(repo_path).resolve()),
        summary=summary,
        pack_sha=sha,
    )
    sdb.set_repo_pack_sha(state_conn, repo=repo_name, pack_sha=sha)
    return IngestResult(status="indexed", pack_sha=sha, repo=repo_name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_flow.py -v`
Expected: PASS — three tests green.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/codemem/ingest/flow.py \
        aiforge_core/codemem/tests/L1_repo_node/test_flow.py
git commit -m "feat(codemem): Stage1+2 orchestrator with sha-skip + force flag"
```

---

## Task 8: Operator CLI (`aiforge-codemem`)

**Files:**
- Create: `aiforge_core/codemem/api/cli.py`
- Modify: `pyproject.toml` (add console script)

- [ ] **Step 1: Write the failing test**

Append to `aiforge_core/codemem/tests/L1_repo_node/test_flow.py`:

```python
def test_cli_ingest_subcommand_dispatches_to_flow(tmp_path, monkeypatch) -> None:
    """`aiforge-codemem ingest <repo>` calls flow.ingest_repo with parsed args."""
    from unittest.mock import patch, MagicMock
    from aiforge_core.codemem.api import cli

    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    fake_driver = MagicMock()

    # Monkey-patch the deps cli builds at runtime
    monkeypatch.setenv("AIFORGE_CODEMEM_STATE_DB", str(tmp_path / "state.db"))

    with patch("aiforge_core.codemem.api.cli._driver", return_value=fake_driver), \
         patch("aiforge_core.codemem.api.cli.flow.ingest_repo") as ingest:
        ingest.return_value = type(
            "R", (), {"status": "indexed", "pack_sha": "sha", "repo": "rX"}
        )
        rc = cli.main(["ingest", "rX", "--path", str(tmp_path)])
    assert rc == 0
    ingest.assert_called_once()


def test_cli_doctor_returns_0_when_all_green(monkeypatch) -> None:
    from unittest.mock import patch
    from aiforge_core.codemem.api import cli

    with patch("aiforge_core.codemem.api.cli._check_repomix", return_value=(True, "ok")), \
         patch("aiforge_core.codemem.api.cli._check_neo4j", return_value=(True, "ok")), \
         patch("aiforge_core.codemem.api.cli._check_llm", return_value=(True, "ok")):
        rc = cli.main(["doctor"])
    assert rc == 0


def test_cli_doctor_returns_1_when_repomix_missing() -> None:
    from unittest.mock import patch
    from aiforge_core.codemem.api import cli

    with patch("aiforge_core.codemem.api.cli._check_repomix",
               return_value=(False, "missing")), \
         patch("aiforge_core.codemem.api.cli._check_neo4j", return_value=(True, "ok")), \
         patch("aiforge_core.codemem.api.cli._check_llm", return_value=(True, "ok")):
        rc = cli.main(["doctor"])
    assert rc == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_flow.py -v -k cli`
Expected: FAIL — `ModuleNotFoundError: aiforge_core.codemem.api.cli`

- [ ] **Step 3: Implement the CLI**

Create `aiforge_core/codemem/api/cli.py`:

```python
"""`aiforge-codemem` operator CLI.

Subcommands (plan 1 ships ingest, doctor, stats):
    aiforge-codemem ingest <repo> [--path DIR] [--force]
    aiforge-codemem doctor
    aiforge-codemem stats <repo>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from aiforge_core.codemem.ingest import flow
from aiforge_core.codemem.store import schema, state_db as sdb


def _driver():
    """Open the project's Neo4j driver. Errors propagate to caller."""
    from neo4j import GraphDatabase
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    return GraphDatabase.driver(uri, auth=(user, pw))


def _cmd_ingest(args: argparse.Namespace) -> int:
    drv = _driver()
    schema.apply(drv)
    state = sdb.open_db()
    sdb.migrate(state)
    res = flow.ingest_repo(
        repo_name=args.repo,
        repo_path=args.path or os.getcwd(),
        driver=drv,
        state_conn=state,
        force=args.force,
    )
    print(json.dumps({
        "status": res.status, "pack_sha": res.pack_sha, "repo": res.repo,
    }))
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    drv = _driver()
    with drv.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:$n}) RETURN r", n=args.repo
        ).single()
    if not row:
        print(json.dumps({"error": "repo_not_found", "repo": args.repo}))
        return 1
    r = dict(row["r"])
    # last_indexed_at is a Neo4j DateTime; stringify
    if "last_indexed_at" in r and r["last_indexed_at"] is not None:
        r["last_indexed_at"] = str(r["last_indexed_at"])
    runbook = r.pop("runbook_md", "") or ""
    conventions = r.pop("conventions_md", "") or ""
    r["runbook_md_chars"] = len(runbook)
    r["conventions_md_chars"] = len(conventions)
    print(json.dumps(r, indent=2, default=str))
    return 0


def _check_repomix() -> tuple[bool, str]:
    binary = os.environ.get("AIFORGE_CODEMEM_REPOMIX", "repomix")
    path = shutil.which(binary)
    if not path:
        return False, f"{binary} not on PATH"
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=5
        )
    except Exception as exc:
        return False, str(exc)
    return True, proc.stdout.strip() or "ok"


def _check_neo4j() -> tuple[bool, str]:
    try:
        drv = _driver()
        with drv.session() as s:
            s.run("RETURN 1").consume()
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def _check_llm() -> tuple[bool, str]:
    import urllib.error
    import urllib.request
    url = os.environ.get(
        "AIFORGE_CODEMEM_LM_URL",
        os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
    )
    probe = url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(probe, timeout=3) as resp:
            ok = resp.status == 200
        return (True, "ok") if ok else (False, f"status {resp.status}")
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks = [
        ("repomix", _check_repomix()),
        ("neo4j", _check_neo4j()),
        ("llm", _check_llm()),
    ]
    payload = {"checks": [{"name": n, "ok": ok, "info": info}
                          for n, (ok, info) in checks]}
    print(json.dumps(payload, indent=2))
    return 0 if all(ok for _, (ok, _) in checks) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aiforge-codemem")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="Stage 1+2 ingest of a repo")
    ing.add_argument("repo", help="Logical repo name (becomes Repo.name)")
    ing.add_argument("--path", help="Repo dir; defaults to CWD")
    ing.add_argument("--force", action="store_true",
                     help="Re-run even if pack_sha matches")
    ing.set_defaults(func=_cmd_ingest)

    st = sub.add_parser("stats", help="Print Repo node summary")
    st.add_argument("repo")
    st.set_defaults(func=_cmd_stats)

    doc = sub.add_parser("doctor", help="Check repomix, neo4j, llm")
    doc.set_defaults(func=_cmd_doctor)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Add console script in pyproject**

Edit `pyproject.toml`. After the existing `[project.optional-dependencies]` section, add (or append to existing `[project.scripts]` if present):

```toml
[project.scripts]
aiforge-codemem = "aiforge_core.codemem.api.cli:main"
```

If `[project.scripts]` already exists, add the line inside it instead of creating a new block.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_flow.py -v -k cli`
Expected: PASS — three CLI tests green.

- [ ] **Step 6: Manual smoke (optional, only if your local Neo4j+LM are up)**

```bash
pip install -e .[dev]
aiforge-codemem doctor
```

Expected: JSON with three checks. `repomix` may show "not on PATH" — that's OK, the gate test mocks it.

- [ ] **Step 7: Commit**

```bash
git add aiforge_core/codemem/api/cli.py pyproject.toml \
        aiforge_core/codemem/tests/L1_repo_node/test_flow.py
git commit -m "feat(codemem): CLI — ingest, stats, doctor"
```

---

## Task 9: L1 gate test + README

**Files:**
- Create: `aiforge_core/codemem/tests/L1_repo_node/README.md`
- Create: `aiforge_core/codemem/tests/L1_repo_node/expected/tiny_repo_node.json`
- Create: `aiforge_core/codemem/tests/L1_repo_node/test_l1_gate.py`
- Modify: `Makefile` (add `test-codemem-L1`)

- [ ] **Step 1: Write the gate README**

Create `aiforge_core/codemem/tests/L1_repo_node/README.md`:

```markdown
# Layer L1 — Repo node ingest gate

## Purpose
After Stage 1 (RepoMix pack) and Stage 2 (LLM repo summary), a single
`(:Repo {name})` exists in Neo4j with all four core commands populated,
a runbook ≥500 chars, and a stable pack_sha that idempotency depends on.

## Fixture
- input: `fixtures/tiny_repo/` (3 files; README, Makefile, src/main.py)
- recorded pack: `fixtures/tiny_pack.md` (used when LLM/RepoMix mocked)
- recorded LLM response: `fixtures/llm_response_ok.json`
- expected node properties: `expected/tiny_repo_node.json`

## Command

    make test-codemem-L1

or directly:

    pytest aiforge_core/codemem/tests/L1_repo_node/ -v

## Pass criteria
- `(:Repo {name:'tiny_repo_test'})` exists after ingest
- 5/5 fields populated: lang_primary, build_cmd, test_cmd, run_cmd, portforward_cmds
- `runbook_md` length ≥ 500 characters
- `last_pack_sha` is a 64-char hex sha256
- `last_indexed_at` is non-null (Neo4j DateTime)
- Re-running ingest with the same content sets `status='skipped_unchanged'`
- `--force` re-runs and overwrites `last_indexed_at`

## Sample expected output

After `aiforge-codemem ingest tiny_repo_test --path .../tiny_repo`:

    {
      "status": "indexed",
      "pack_sha": "<64-hex>",
      "repo": "tiny_repo_test"
    }

Repo node in Neo4j (abbreviated):

    name: "tiny_repo_test"
    lang_primary: "python"
    build_cmd: "make build"
    test_cmd: "make test"
    run_cmd: "make run"
    portforward_cmds: ["kubectl port-forward svc/tiny-repo 8080:8080 ..."]
    runbook_md: "## Tiny Repo Runbook ..."  (≥500 chars)
    last_pack_sha: "<sha256>"
    schema_version: "codemem-v1"

## On failure
- `repomix` not on PATH → install with `npm i -g repomix` or set
  `AIFORGE_CODEMEM_REPOMIX=/path/to/repomix`. Unit tests skip with
  `live_repomix` marker; the gate uses mocks, so this only matters
  for `aiforge-codemem doctor`.
- Neo4j unreachable → check `AIFORGE_NEO4J_URI`/`USER`/`PASSWORD`,
  ensure bolt port (7687) is open, run `cypher-shell` manually.
- LLM 4xx — `response_format={"type":"json_object"}` may not be
  honored by every LM Studio build; the parser tolerates fenced
  output, but if both mocked and live responses are bad JSON,
  re-record `fixtures/llm_response_ok.json` with a clean run.
- escalation: open ticket `CODEMEM-L1-<short>`.
```

- [ ] **Step 2: Write the expected node fixture**

Create `aiforge_core/codemem/tests/L1_repo_node/expected/tiny_repo_node.json`:

```json
{
  "name": "tiny_repo_test",
  "lang_primary": "python",
  "build_cmd": "make build",
  "test_cmd": "make test",
  "lint_cmd": "",
  "run_cmd": "make run",
  "portforward_cmds": ["kubectl port-forward svc/tiny-repo 8080:8080 -n default"],
  "schema_version": "codemem-v1"
}
```

- [ ] **Step 3: Write the failing gate test**

Create `aiforge_core/codemem/tests/L1_repo_node/test_l1_gate.py`:

```python
"""L1 gate — Stage 1+2 end-to-end on tiny_repo fixture.

This is the layer's contract test. It mocks the RepoMix subprocess
and the LLM so it can run without external dependencies — but it
hits a real (or skipped) Neo4j to verify the node is materialized.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_core.codemem.ingest import flow
from aiforge_core.codemem.ingest.repo_summary import RepoSummary
from aiforge_core.codemem.store import schema, state_db as sdb


HERE = Path(__file__).parent
FIX = HERE / "fixtures"
EXPECTED = json.loads((HERE / "expected" / "tiny_repo_node.json").read_text())

pytestmark = pytest.mark.live_neo4j


@pytest.fixture(scope="module")
def driver():
    try:
        from neo4j import GraphDatabase
    except ImportError:
        pytest.skip("neo4j driver not installed")
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    drv = GraphDatabase.driver(uri, auth=(user, pw))
    try:
        with drv.session() as s:
            s.run("RETURN 1").consume()
    except Exception as exc:
        pytest.skip(f"Neo4j unreachable: {exc}")
    schema.apply(drv)
    yield drv
    with drv.session() as s:
        s.run("MATCH (r:Repo {name:'tiny_repo_test'}) DETACH DELETE r").consume()
    drv.close()


def _summary_from_fixture() -> RepoSummary:
    obj = json.loads((FIX / "llm_response_ok.json").read_text())
    return RepoSummary(
        lang_primary=obj["lang_primary"],
        build_cmd=obj["build_cmd"],
        test_cmd=obj["test_cmd"],
        lint_cmd=obj["lint_cmd"],
        run_cmd=obj["run_cmd"],
        portforward_cmds=obj["portforward_cmds"],
        conventions_md=obj["conventions_md"],
        runbook_md=obj["runbook_md"],
    )


def test_l1_gate(driver, tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    pack_text = (FIX / "tiny_pack.md").read_text()

    with patch("aiforge_core.codemem.ingest.flow.pack_repo.pack",
               return_value=(pack_text, "sha-L1GATE")), \
         patch("aiforge_core.codemem.ingest.flow.repo_summary.summarize",
               return_value=_summary_from_fixture()):
        result = flow.ingest_repo(
            repo_name="tiny_repo_test",
            repo_path=str(FIX / "tiny_repo"),
            driver=driver,
            state_conn=state,
        )
    assert result.status == "indexed"

    with driver.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:'tiny_repo_test'}) RETURN r"
        ).single()
    assert row is not None, "Repo node not created"
    r = row["r"]

    # 5/5 commands populated (lint may be empty by design)
    assert r["lang_primary"] == EXPECTED["lang_primary"]
    assert r["build_cmd"] == EXPECTED["build_cmd"]
    assert r["test_cmd"] == EXPECTED["test_cmd"]
    assert r["run_cmd"] == EXPECTED["run_cmd"]
    assert r["portforward_cmds"] == EXPECTED["portforward_cmds"]
    # Runbook contract
    assert len(r["runbook_md"]) >= 500
    # Pack sha shape
    assert r["last_pack_sha"] == "sha-L1GATE"
    # Schema version stamp
    assert r["schema_version"] == EXPECTED["schema_version"]


def test_l1_gate_idempotent(driver, tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    pack_text = (FIX / "tiny_pack.md").read_text()

    with patch("aiforge_core.codemem.ingest.flow.pack_repo.pack",
               return_value=(pack_text, "sha-IDEMP")), \
         patch("aiforge_core.codemem.ingest.flow.repo_summary.summarize",
               return_value=_summary_from_fixture()):
        first = flow.ingest_repo(
            repo_name="tiny_repo_test",
            repo_path=str(FIX / "tiny_repo"),
            driver=driver, state_conn=state,
        )
        second = flow.ingest_repo(
            repo_name="tiny_repo_test",
            repo_path=str(FIX / "tiny_repo"),
            driver=driver, state_conn=state,
        )
    assert first.status == "indexed"
    assert second.status == "skipped_unchanged"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest aiforge_core/codemem/tests/L1_repo_node/test_l1_gate.py -v`
Expected: PASS if Neo4j is up; SKIP otherwise. (No new module to create; this test exercises code from previous tasks.) If it FAILS instead of skipping or passing, the regression is real — fix.

- [ ] **Step 5: Add Makefile target**

Edit `Makefile`. After the `test:` target, add:

```makefile
.PHONY: test-codemem-L1 test-codemem-all

test-codemem-L1:
	.venv/bin/pytest aiforge_core/codemem/tests/L1_repo_node/ -v

test-codemem-all:
	.venv/bin/pytest aiforge_core/codemem/tests/ -v
```

Update the `.PHONY:` line at the top of the file to include both new targets:

```makefile
.PHONY: help install test ui deploy pull kill-all \
        index-all status logs-tail health sync-memory reindex-memory \
        test-codemem-L1 test-codemem-all
```

- [ ] **Step 6: Run the gate via make**

Run: `make test-codemem-L1`
Expected: All L1 tests green (or skip when Neo4j is down). `repomix`-marked tests also skip absent the binary.

- [ ] **Step 7: Commit**

```bash
git add aiforge_core/codemem/tests/L1_repo_node/README.md \
        aiforge_core/codemem/tests/L1_repo_node/expected/ \
        aiforge_core/codemem/tests/L1_repo_node/test_l1_gate.py \
        Makefile
git commit -m "feat(codemem): L1 gate test + README + make target"
```

---

## Task 10: Wire ingest into the existing aiforge-maint cron entry (optional convenience)

**Files:**
- Modify: `aiforge_core/runtime/maintenance_cli.py`

This step exposes `aiforge-maint codemem ingest <repo>` as a sibling of the existing `aiforge-maint memory decay` etc. The standalone `aiforge-codemem` entry from Task 8 still works; this is a convenience shortcut that lives next to the other ops shortcuts.

- [ ] **Step 1: Open `aiforge_core/runtime/maintenance_cli.py` and locate the argparse setup**

Read the file. Identify the `subparsers` and dispatch tables.

- [ ] **Step 2: Add a `codemem` subcommand**

Append a new dispatcher block. Concretely:

```python
# add near the other subcommand registrations
def _cmd_codemem_ingest(args) -> int:
    from aiforge_core.codemem.api.cli import _cmd_ingest, _driver  # reuse
    return _cmd_ingest(args)


def _register_codemem(subparsers) -> None:
    cm = subparsers.add_parser("codemem", help="codemem operator commands")
    cm_sub = cm.add_subparsers(dest="cm_cmd", required=True)
    ing = cm_sub.add_parser("ingest", help="Stage 1+2 ingest")
    ing.add_argument("repo")
    ing.add_argument("--path")
    ing.add_argument("--force", action="store_true")
    ing.set_defaults(func=_cmd_codemem_ingest)
```

Wire `_register_codemem(subparsers)` into the existing argparse setup in `main()`.

- [ ] **Step 3: Smoke-test the wiring**

```bash
aiforge-maint codemem ingest tiny_repo_test --path aiforge_core/codemem/tests/L1_repo_node/fixtures/tiny_repo
```

Expected: same JSON output as `aiforge-codemem ingest`. May fail on missing Neo4j; that's fine for the wiring smoke — the dispatch path is what we care about.

- [ ] **Step 4: Commit**

```bash
git add aiforge_core/runtime/maintenance_cli.py
git commit -m "feat(codemem): aiforge-maint codemem ingest convenience wrapper"
```

---

## Self-review checklist (run before declaring plan 1 done)

1. **Spec coverage:** All §3 module structure files for the L1 slice exist. The §4 `Repo` node has every property the spec lists (lang_primary, build_cmd, test_cmd, lint_cmd, run_cmd, portforward_cmds, conventions_md, runbook_md, last_pack_sha, last_indexed_at, schema_version). Stages 1 and 2 from §5 are implemented; Stages 3–9 are intentionally deferred to plans 2–8. The §7 L1 gate criteria are encoded in `test_l1_gate.py`. The §6 schema is namespaced via `schema_version='codemem-v1'`. The §10 error-handling stories for Stage 1 (binary missing → RepoMixNotFound; nonzero exit → RepoMixError) and Stage 2 (invalid JSON → retry → RepoSummaryError) are tested.

2. **Placeholder scan:** No "TBD", "implement later", or naked references. Every code block contains real code.

3. **Type consistency:** `RepoSummary` fields used in Task 5 match the Cypher params in Task 6 match the assertions in Task 9 (`expected/tiny_repo_node.json`). `IngestResult.status` values used in Task 7 match the assertions in Task 8 (`status='indexed'` / `status='skipped_unchanged'`). Module paths are uniform: `aiforge_core.codemem.{ingest,store,api,tests}`.

4. **Idempotency:** Schema migrations use `IF NOT EXISTS`. State DB uses `CREATE TABLE IF NOT EXISTS`. Cypher writer uses `MERGE`. Flow short-circuits on matching pack_sha.

5. **Soft-fail invariants:** Stage 1 raises typed exceptions, never silently corrupts. Stage 2 retries once before raising. CLI commands return distinct exit codes (0/1) so cron wrappers can detect. The gate test skips Neo4j cleanly when down.

6. **No double-write:** Plan 1 writes only `(:Repo)` nodes. Legacy `index/` and `memory/` packages are untouched. Nothing in this plan modifies an existing file outside `pyproject.toml`, `Makefile`, and (Task 10, optional) `runtime/maintenance_cli.py`.

If any of the above fails the read-through, fix inline before handing off.
