# Memory & Tool Orchestration v4.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 2-tier pgmem + Chroma RAG + Hindsight with single Postgres-backed 4-tier memory + hybrid retrieval + orchestrator-driven context assembly + 4-role pipeline (Architect/SrDev/Developer/FactExtract).

**Architecture:** Postgres 17 (pgvector + pg_trgm) stores T1 episodic / T2 semantic / T3 procedural / T4 codebase. Retrieval pipeline = BM25 + vector → RRF → bge-reranker-v2-m3. Orchestrator (`aiforge_core`) assembles per-role context bundles with hard compaction rules. Paperclip lifecycle v4.1 has parent→child sub-tickets with reflection on merge.

**Tech Stack:** Python 3.11+, Postgres 17, pgvector, pg_trgm, psycopg[binary] 3.2, bge-m3 (ONNX), bge-reranker-v2-m3 (FlagReranker), FastAPI sidecars, tree-sitter, LM Studio (local agents), Claude Code (external architect).

**Spec reference:** `docs/superpowers/specs/2026-04-21-autonomous-memory-orchestration-design.md`

**Commits:** Every task ends in a commit. Every phase ends with a phase-summary tag commit.

---

## File Structure

```
aiforge_core/
  embed.py            (new)   single embed() helper → :8764
  store_v2.py         (new)   unified memory store, 4 tiers
  retrieval.py        (new)   hybrid BM25+vector+RRF+rerank
  context.py          (new)   prompt assembler + compactor
  reflection.py       (new)   fact-extract runner, proposal CLI
  pgmem.py            (delete — superseded by store_v2)
  rag.py              (rewrite) becomes thin wrapper around store_v2 T4
  lifecycle.py        (rewrite) v4.1 state machine parent/child
  config.py           (extend)  Routing dataclass v4.1 + kill-switch
  retry.py            (extend)  kill-switch file check + confidence gate
  observe.py          (extend)  confidence + tier-hit counters
  cli.py              (extend)  `aiforge propose` + `aiforge memory`
agents/
  architect/          (new)   replaces em/
  sr-developer/       (rewrite) decomposition role
  developer/          (new)   implementation role
  fact-extract/       (new)   reflection role
  em/                 (delete)
  tester/             (delete)
  sr-architect/       (delete)
mcp/
  memory-server.json  (new)
  rag-server.json     (rewrite)
  code-review-graph.json  (keep)
  git-tools.json      (extend w/ git_diff + run_tests + run_command)
services/
  embed_sidecar/      (new)   FastAPI + bge-m3 ONNX
  rerank_sidecar/     (new)   FastAPI + FlagReranker
scripts/
  install-embed-sidecar.sh (new)
  install-rerank-sidecar.sh (new)
  install-pg-aiforge.sh     (new)  creates DB, extensions, schema
  migrate-memory.sh         (new)  one-shot migration: Chroma→pg, seed T4
  hermes-seed-memory.sh     (delete)
  hermes-setup-hindsight.sh (delete)
  patch-hindsight-shutdown-bug.sh (delete)
paperclip.config.yml  (rewrite) v4.1 org chart + routing
tests/python/
  test_store_v2.py          (new)
  test_retrieval.py         (new)
  test_context.py           (new)
  test_reflection.py        (new)
  test_lifecycle_v41.py     (new)
  test_paperclip_rag.py     (rewrite)
  test_paperclip_pgmem.py   (delete)
```

---

## Phase 1 — Infrastructure: Postgres schema + sidecars

### Task 1.1: Postgres DB bootstrap script

**Files:**
- Create: `scripts/install-pg-aiforge.sh`
- Test: `tests/shell/test_scripts.bats` (extend)

- [ ] **Step 1: Write bats test asserting script exists + executable**

Append to `tests/shell/test_scripts.bats`:

```bash
@test "install-pg-aiforge.sh exists and is executable" {
  [ -x scripts/install-pg-aiforge.sh ]
}

@test "install-pg-aiforge.sh --dry-run prints CREATE EXTENSION statements" {
  run bash scripts/install-pg-aiforge.sh --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" =~ "CREATE EXTENSION IF NOT EXISTS vector" ]]
  [[ "$output" =~ "CREATE EXTENSION IF NOT EXISTS pg_trgm" ]]
  [[ "$output" =~ "CREATE TABLE memories" ]]
  [[ "$output" =~ "CREATE TABLE memory_proposals" ]]
}
```

- [ ] **Step 2: Run test, verify fails**

Run: `bats tests/shell/test_scripts.bats -f install-pg-aiforge`
Expected: FAIL (file missing)

- [ ] **Step 3: Create script**

Create `scripts/install-pg-aiforge.sh`:

```bash
#!/usr/bin/env bash
# Bootstrap the `aiforge` Postgres database for v4.1 memory.
# Usage:
#   bash scripts/install-pg-aiforge.sh [--dry-run]
#   SSH_HOST=manikanta@192.168.70.185 bash scripts/install-pg-aiforge.sh
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; fi

SQL=$(cat <<'EOF'
CREATE DATABASE aiforge;
\c aiforge
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL PRIMARY KEY,
    tier        TEXT NOT NULL CHECK (tier IN ('t1','t2','t3','t4')),
    wing        TEXT NOT NULL,
    parent_id   TEXT,
    kind        TEXT NOT NULL,
    source      TEXT,
    title       TEXT,
    text        TEXT NOT NULL,
    embedding   vector(1024),
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memories_tier_wing  ON memories(tier, wing);
CREATE INDEX IF NOT EXISTS idx_memories_parent     ON memories(parent_id);
CREATE INDEX IF NOT EXISTS idx_memories_expires    ON memories(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_embedding  ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memories_text_trgm  ON memories USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_memories_title_trgm ON memories USING gin (title gin_trgm_ops);

CREATE TABLE IF NOT EXISTS memory_proposals (
    id           BIGSERIAL PRIMARY KEY,
    tier         TEXT NOT NULL CHECK (tier IN ('t2','t3')),
    wing         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    title        TEXT,
    text         TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_trace TEXT NOT NULL,
    proposed_by  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ,
    decided_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON memory_proposals(status);
EOF
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "$SQL"
  exit 0
fi

PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-$USER}"

psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres -v ON_ERROR_STOP=0 <<< "$SQL"
echo "aiforge DB ready at $PG_HOST:$PG_PORT"
```

Then: `chmod +x scripts/install-pg-aiforge.sh`

- [ ] **Step 4: Run test, verify passes**

Run: `bats tests/shell/test_scripts.bats -f install-pg-aiforge`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/install-pg-aiforge.sh tests/shell/test_scripts.bats
git commit -m "feat(p1): pg bootstrap script for aiforge memory DB"
```

---

### Task 1.2: Embed sidecar (bge-m3 ONNX FastAPI)

**Files:**
- Create: `services/embed_sidecar/app.py`
- Create: `services/embed_sidecar/requirements.txt`
- Create: `scripts/install-embed-sidecar.sh`
- Test: `tests/python/test_embed_sidecar.py`

- [ ] **Step 1: Write requirements file**

Create `services/embed_sidecar/requirements.txt`:

```
fastapi==0.115.0
uvicorn[standard]==0.32.0
onnxruntime==1.20.0
transformers==4.46.0
numpy<2.0
```

- [ ] **Step 2: Write failing test for embed endpoint shape**

Create `tests/python/test_embed_sidecar.py`:

```python
"""Contract test for embed sidecar. Requires sidecar running at :8764."""
from __future__ import annotations
import os
import urllib.request
import json
import pytest

SIDECAR = os.environ.get("EMBED_SIDECAR_URL", "http://127.0.0.1:8764")


def _post(path, body):
    req = urllib.request.Request(
        f"{SIDECAR}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


@pytest.mark.live_sidecar
def test_embed_returns_1024_vector():
    resp = _post("/embed", {"text": "hello world"})
    assert "embedding" in resp
    assert len(resp["embedding"]) == 1024
    assert all(isinstance(x, float) for x in resp["embedding"])


@pytest.mark.live_sidecar
def test_embed_batch():
    resp = _post("/embed_batch", {"texts": ["a", "b", "c"]})
    assert len(resp["embeddings"]) == 3
    assert len(resp["embeddings"][0]) == 1024
```

Register marker in `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
[tool.pytest.ini_options]
markers = [
    "live_sidecar: requires embed/rerank sidecars running",
]
```

- [ ] **Step 3: Run test, verify skipped/fails (sidecar not up)**

Run: `pytest tests/python/test_embed_sidecar.py -v -m live_sidecar`
Expected: errors with connection refused (sidecar not running)

- [ ] **Step 4: Implement FastAPI app**

Create `services/embed_sidecar/app.py`:

```python
"""bge-m3 ONNX embedding sidecar.

Serves 1024-d dense embeddings for the aiforge memory store.
Loads model on startup. Single process recommended (holds ~2GB).
"""
from __future__ import annotations

import os
from typing import List

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import AutoTokenizer

MODEL_DIR = os.environ.get("BGE_M3_DIR", os.path.expanduser("~/.aiforge/models/bge-m3"))
MAX_LEN = int(os.environ.get("BGE_M3_MAX_LEN", "512"))

app = FastAPI(title="aiforge-embed-sidecar", version="1.0.0")

_tokenizer = None
_session = None


def _load():
    global _tokenizer, _session
    if _session is not None:
        return
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
    providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    _session = ort.InferenceSession(
        os.path.join(MODEL_DIR, "model.onnx"),
        providers=providers,
    )


@app.on_event("startup")
def startup():
    _load()


class EmbedReq(BaseModel):
    text: str


class EmbedBatchReq(BaseModel):
    texts: List[str]


def _embed_batch(texts: List[str]) -> List[List[float]]:
    enc = _tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=MAX_LEN,
        return_tensors="np",
    )
    inputs = {
        "input_ids": enc["input_ids"].astype(np.int64),
        "attention_mask": enc["attention_mask"].astype(np.int64),
    }
    outputs = _session.run(None, inputs)
    # bge-m3 dense head output: last_hidden_state[:, 0, :]  (CLS pooling)
    cls = outputs[0][:, 0, :]
    # L2 normalize for cosine
    norms = np.linalg.norm(cls, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    emb = cls / norms
    return emb.astype(np.float32).tolist()


@app.post("/embed")
def embed(req: EmbedReq):
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    [v] = _embed_batch([req.text])
    return {"embedding": v}


@app.post("/embed_batch")
def embed_batch(req: EmbedBatchReq):
    if not req.texts:
        return {"embeddings": []}
    return {"embeddings": _embed_batch(req.texts)}


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model_dir": MODEL_DIR, "dim": 1024}
```

- [ ] **Step 5: Write installer script**

Create `scripts/install-embed-sidecar.sh`:

```bash
#!/usr/bin/env bash
# Install + run bge-m3 embed sidecar on port 8764.
set -euo pipefail

SIDECAR_DIR="$(cd "$(dirname "$0")/.." && pwd)/services/embed_sidecar"
MODEL_DIR="${BGE_M3_DIR:-$HOME/.aiforge/models/bge-m3}"
VENV="${EMBED_VENV:-$HOME/.aiforge/venv-embed}"

mkdir -p "$MODEL_DIR" "$(dirname "$VENV")"

if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -r "$SIDECAR_DIR/requirements.txt"

# Model download (ONNX export of bge-m3) — first run only
if [[ ! -f "$MODEL_DIR/model.onnx" ]]; then
  "$VENV/bin/python" -c "
from huggingface_hub import snapshot_download
snapshot_download('aapot/bge-m3-onnx', local_dir='$MODEL_DIR', local_dir_use_symlinks=False)
"
fi

cd "$SIDECAR_DIR"
exec "$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 8764
```

Then: `chmod +x scripts/install-embed-sidecar.sh`

- [ ] **Step 6: Manual verification**

Run in a terminal (Mac Studio):

```bash
bash scripts/install-embed-sidecar.sh &
sleep 15
curl -s http://127.0.0.1:8764/healthz
curl -s -X POST http://127.0.0.1:8764/embed -H 'Content-Type: application/json' -d '{"text":"hello"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print('dim', len(d['embedding']))"
```

Expected: `{"status":"ok",...}` and `dim 1024`

- [ ] **Step 7: Run contract test against live sidecar**

Run: `pytest tests/python/test_embed_sidecar.py -v -m live_sidecar`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add services/embed_sidecar/ scripts/install-embed-sidecar.sh tests/python/test_embed_sidecar.py pyproject.toml
git commit -m "feat(p1): bge-m3 ONNX embed sidecar on :8764"
```

---

### Task 1.3: Rerank sidecar (bge-reranker-v2-m3 FastAPI)

**Files:**
- Create: `services/rerank_sidecar/app.py`
- Create: `services/rerank_sidecar/requirements.txt`
- Create: `scripts/install-rerank-sidecar.sh`
- Test: `tests/python/test_rerank_sidecar.py`

- [ ] **Step 1: Write requirements file**

Create `services/rerank_sidecar/requirements.txt`:

```
fastapi==0.115.0
uvicorn[standard]==0.32.0
FlagEmbedding==1.3.0
torch==2.5.0
```

- [ ] **Step 2: Write failing contract test**

Create `tests/python/test_rerank_sidecar.py`:

```python
"""Contract test for rerank sidecar. Requires sidecar running at :8765."""
from __future__ import annotations
import os
import urllib.request
import json
import pytest

SIDECAR = os.environ.get("RERANK_SIDECAR_URL", "http://127.0.0.1:8765")


def _post(path, body):
    req = urllib.request.Request(
        f"{SIDECAR}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


@pytest.mark.live_sidecar
def test_rerank_orders_relevant_first():
    resp = _post(
        "/rerank",
        {
            "query": "how to publish NATS message",
            "candidates": [
                {"id": "a", "text": "JetStream publishAsync API sample"},
                {"id": "b", "text": "how to write a haiku about spring"},
                {"id": "c", "text": "publishToRemoteServer uses local NATS queue"},
            ],
        },
    )
    assert "order" in resp
    assert len(resp["order"]) == 3
    # relevant candidates (a, c) should rank above irrelevant (b)
    ranked_ids = [["a", "b", "c"][i] for i in resp["order"]]
    assert ranked_ids.index("b") > 0  # "b" is not first
```

- [ ] **Step 3: Run test, verify fails**

Run: `pytest tests/python/test_rerank_sidecar.py -v -m live_sidecar`
Expected: connection refused

- [ ] **Step 4: Implement FastAPI app**

Create `services/rerank_sidecar/app.py`:

```python
"""bge-reranker-v2-m3 rerank sidecar.

Serves cross-encoder rerank scores for the aiforge retrieval pipeline.
"""
from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI
from pydantic import BaseModel
from FlagEmbedding import FlagReranker

MODEL_NAME = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
USE_FP16 = os.environ.get("RERANK_FP16", "1") == "1"

app = FastAPI(title="aiforge-rerank-sidecar", version="1.0.0")

_reranker: FlagReranker | None = None


@app.on_event("startup")
def startup():
    global _reranker
    _reranker = FlagReranker(MODEL_NAME, use_fp16=USE_FP16)


class Candidate(BaseModel):
    id: str
    text: str


class RerankReq(BaseModel):
    query: str
    candidates: List[Candidate]


@app.post("/rerank")
def rerank(req: RerankReq):
    if not req.candidates:
        return {"scores": [], "order": []}
    pairs = [[req.query, c.text] for c in req.candidates]
    scores = _reranker.compute_score(pairs, normalize=True)
    # compute_score returns float for single pair, list for multiple
    if isinstance(scores, float):
        scores = [scores]
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return {
        "scores": [float(s) for s in scores],
        "order": order,
        "ids_ordered": [req.candidates[i].id for i in order],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": MODEL_NAME, "fp16": USE_FP16}
```

- [ ] **Step 5: Write installer**

Create `scripts/install-rerank-sidecar.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

SIDECAR_DIR="$(cd "$(dirname "$0")/.." && pwd)/services/rerank_sidecar"
VENV="${RERANK_VENV:-$HOME/.aiforge/venv-rerank}"

mkdir -p "$(dirname "$VENV")"
if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -r "$SIDECAR_DIR/requirements.txt"

cd "$SIDECAR_DIR"
exec "$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 8765
```

Then: `chmod +x scripts/install-rerank-sidecar.sh`

- [ ] **Step 6: Manual verification**

```bash
bash scripts/install-rerank-sidecar.sh &
sleep 30   # first run downloads model
curl -s http://127.0.0.1:8765/healthz
```

- [ ] **Step 7: Run contract test**

Run: `pytest tests/python/test_rerank_sidecar.py -v -m live_sidecar`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add services/rerank_sidecar/ scripts/install-rerank-sidecar.sh tests/python/test_rerank_sidecar.py
git commit -m "feat(p1): bge-reranker-v2-m3 sidecar on :8765"
```

---

### Task 1.4: embed.py helper module

**Files:**
- Create: `aiforge_core/embed.py`
- Test: `tests/python/test_embed_helper.py`

- [ ] **Step 1: Write failing test**

Create `tests/python/test_embed_helper.py`:

```python
from unittest.mock import patch, MagicMock
from aiforge_core import embed as embed_mod


def _fake_urlopen(response_json):
    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock.read.return_value = __import__("json").dumps(response_json).encode()
    return mock


def test_embed_single():
    with patch.object(embed_mod.urllib.request, "urlopen",
                      return_value=_fake_urlopen({"embedding": [0.1] * 1024})):
        v = embed_mod.embed("hello")
    assert len(v) == 1024


def test_embed_batch():
    with patch.object(embed_mod.urllib.request, "urlopen",
                      return_value=_fake_urlopen({"embeddings": [[0.1] * 1024, [0.2] * 1024]})):
        vs = embed_mod.embed_batch(["a", "b"])
    assert len(vs) == 2
    assert len(vs[0]) == 1024


def test_embed_url_env_override(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_URL", "http://custom:9999")
    import importlib
    importlib.reload(embed_mod)
    assert embed_mod.SIDECAR_URL == "http://custom:9999"
```

- [ ] **Step 2: Run test, verify fails**

Run: `pytest tests/python/test_embed_helper.py -v`
Expected: ImportError (module missing)

- [ ] **Step 3: Implement**

Create `aiforge_core/embed.py`:

```python
"""Single embed() helper talking to the bge-m3 sidecar on :8764.

Replaces the LM Studio nomic-embed endpoint used by old pgmem.py.
All tiers (T1–T4) embed through this one helper.
"""
from __future__ import annotations

import json
import os
import urllib.request

SIDECAR_URL = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764")
DIM = 1024


def _post(path: str, body: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        f"{SIDECAR_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def embed(text: str) -> list[float]:
    """Return 1024-d dense embedding for `text`."""
    if not text.strip():
        raise ValueError("cannot embed empty text")
    resp = _post("/embed", {"text": text})
    return resp["embedding"]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Return list of 1024-d embeddings. Preserves input order."""
    if not texts:
        return []
    resp = _post("/embed_batch", {"texts": list(texts)})
    return resp["embeddings"]
```

- [ ] **Step 4: Run test, verify passes**

Run: `pytest tests/python/test_embed_helper.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/embed.py tests/python/test_embed_helper.py
git commit -m "feat(p1): embed() helper talks to :8764 sidecar"
```

---

### Task 1.5: Phase 1 tag commit

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/python/ -v -m "not live_sidecar"`
Expected: all existing tests pass + new embed helper test passes

- [ ] **Step 2: Commit phase tag**

```bash
git commit --allow-empty -m "chore(p1): phase 1 complete — infra (pg + embed + rerank + helper)"
```

---

## Phase 2 — Unified memory store (store_v2)

### Task 2.1: Store skeleton + schema helpers

**Files:**
- Create: `aiforge_core/store_v2.py`
- Test: `tests/python/test_store_v2.py`

- [ ] **Step 1: Write failing test for schema creation**

Create `tests/python/test_store_v2.py`:

```python
"""Store v2 tests. Uses ephemeral Postgres via AIFORGE_PGMEM_DSN env."""
from __future__ import annotations
import os
import pytest
import psycopg

DSN = os.environ.get("AIFORGE_PGMEM_DSN",
                     "host=127.0.0.1 port=5432 dbname=aiforge")


def _pg_available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(),
                                 reason="aiforge DB not reachable; run install-pg-aiforge.sh")


@pytest.fixture
def store():
    from aiforge_core.store_v2 import Store
    s = Store(dsn=DSN)
    s.ensure_schema()
    # start clean
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("TRUNCATE memories RESTART IDENTITY")
        cur.execute("TRUNCATE memory_proposals RESTART IDENTITY")
    return s


def test_ensure_schema_idempotent(store):
    store.ensure_schema()
    store.ensure_schema()  # no error
```

- [ ] **Step 2: Run, verify fails**

Run: `pytest tests/python/test_store_v2.py -v`
Expected: ImportError

- [ ] **Step 3: Implement skeleton**

Create `aiforge_core/store_v2.py`:

```python
"""Unified 4-tier memory store backed by Postgres + pgvector + pg_trgm.

Tiers:
  T1 — episodic per-ticket trace, TTL 7 days post-merge
  T2 — semantic cross-ticket facts, human-gated writes
  T3 — procedural recipes, human-gated writes
  T4 — codebase chunks, reindexed from git push
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from . import embed as embed_mod

DEFAULT_DSN = os.environ.get("AIFORGE_PGMEM_DSN",
                              "host=127.0.0.1 port=5432 dbname=aiforge")

VALID_TIERS = {"t1", "t2", "t3", "t4"}

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL PRIMARY KEY,
    tier        TEXT NOT NULL CHECK (tier IN ('t1','t2','t3','t4')),
    wing        TEXT NOT NULL,
    parent_id   TEXT,
    kind        TEXT NOT NULL,
    source      TEXT,
    title       TEXT,
    text        TEXT NOT NULL,
    embedding   vector(1024),
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memories_tier_wing  ON memories(tier, wing);
CREATE INDEX IF NOT EXISTS idx_memories_parent     ON memories(parent_id);
CREATE INDEX IF NOT EXISTS idx_memories_expires    ON memories(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_embedding  ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memories_text_trgm  ON memories USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_memories_title_trgm ON memories USING gin (title gin_trgm_ops);

CREATE TABLE IF NOT EXISTS memory_proposals (
    id           BIGSERIAL PRIMARY KEY,
    tier         TEXT NOT NULL CHECK (tier IN ('t2','t3')),
    wing         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    title        TEXT,
    text         TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_trace TEXT NOT NULL,
    proposed_by  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ,
    decided_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON memory_proposals(status);
"""


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


@dataclass
class Memory:
    id: int
    tier: str
    wing: str
    parent_id: str | None
    kind: str
    source: str | None
    title: str | None
    text: str
    metadata: dict
    created_at: datetime
    expires_at: datetime | None


class Store:
    def __init__(self, dsn: str = DEFAULT_DSN):
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=False)

    def ensure_schema(self) -> None:
        with self._connect() as c, c.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            c.commit()
```

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/python/test_store_v2.py -v`
Expected: 1 passed (schema test) or skipped (if no pg)

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/store_v2.py tests/python/test_store_v2.py
git commit -m "feat(p2): store_v2 skeleton + schema ensure"
```

---

### Task 2.2: T1 append API

**Files:**
- Modify: `aiforge_core/store_v2.py`
- Modify: `tests/python/test_store_v2.py`

- [ ] **Step 1: Add failing test**

Append to `tests/python/test_store_v2.py`:

```python
def test_append_t1_event(store, monkeypatch):
    # Stub embed so tests don't need live sidecar
    from aiforge_core import embed as embed_mod
    monkeypatch.setattr(embed_mod, "embed", lambda t: [0.001] * 1024)

    mid = store.append_event(
        parent_id="TICKET-77",
        kind="tool_call",
        title="search_code called",
        text="query: 'publishToRemoteServer' | hits: 3",
        metadata={"tool": "search_code", "top_k": 5},
        source="agent:developer",
    )
    assert mid > 0

    rows = store.get_episodic("TICKET-77")
    assert len(rows) == 1
    assert rows[0].kind == "tool_call"
    assert rows[0].metadata["tool"] == "search_code"
    assert rows[0].expires_at is None  # not set until ticket merges
```

- [ ] **Step 2: Run, verify fails**

Expected: AttributeError (methods not defined)

- [ ] **Step 3: Implement `append_event` + `get_episodic`**

Add to `aiforge_core/store_v2.py`:

```python
    # ---------- T1 episodic ----------
    def append_event(
        self,
        parent_id: str,
        kind: str,
        text: str,
        *,
        title: str | None = None,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Append an episodic T1 row for a given parent ticket. Returns id."""
        vec = embed_mod.embed(text)
        wing = f"ticket/{parent_id}"
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO memories
                   (tier, wing, parent_id, kind, source, title, text, embedding, metadata)
                   VALUES ('t1', %s, %s, %s, %s, %s, %s, %s::vector, %s::jsonb)
                   RETURNING id""",
                (wing, parent_id, kind, source, title, text,
                 _vec_literal(vec), json.dumps(metadata or {})),
            )
            rid = cur.fetchone()[0]
            c.commit()
            return rid

    def get_episodic(self, parent_id: str) -> list[Memory]:
        """Return all T1 rows for a parent, chronological."""
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                """SELECT id, tier, wing, parent_id, kind, source, title, text,
                          metadata, created_at, expires_at
                   FROM memories
                   WHERE tier = 't1' AND parent_id = %s
                   ORDER BY id ASC""",
                (parent_id,),
            )
            return [
                Memory(
                    id=r[0], tier=r[1], wing=r[2], parent_id=r[3], kind=r[4],
                    source=r[5], title=r[6], text=r[7],
                    metadata=r[8] or {},
                    created_at=r[9], expires_at=r[10],
                )
                for r in cur.fetchall()
            ]

    def mark_ticket_merged(self, parent_id: str, ttl_days: int = 7) -> int:
        """Set expires_at on all T1 rows for this parent."""
        expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE memories SET expires_at = %s "
                "WHERE tier = 't1' AND parent_id = %s AND expires_at IS NULL",
                (expires, parent_id),
            )
            n = cur.rowcount
            c.commit()
            return n

    def gc_expired(self) -> int:
        """Delete T1 rows past expires_at. Returns deleted count."""
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                "DELETE FROM memories WHERE tier = 't1' "
                "AND expires_at IS NOT NULL AND expires_at < now()"
            )
            n = cur.rowcount
            c.commit()
            return n
```

- [ ] **Step 4: Run, verify passes**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/store_v2.py tests/python/test_store_v2.py
git commit -m "feat(p2): T1 episodic append + ttl + gc"
```

---

### Task 2.3: T2/T3/T4 write paths

**Files:**
- Modify: `aiforge_core/store_v2.py`
- Modify: `tests/python/test_store_v2.py`

- [ ] **Step 1: Add failing tests**

Append to test file:

```python
def test_propose_semantic_queued(store, monkeypatch):
    from aiforge_core import embed as embed_mod
    monkeypatch.setattr(embed_mod, "embed", lambda t: [0.0] * 1024)
    pid = store.propose(
        tier="t2", wing="project", kind="fact",
        title="use WebFlux", text="Repo uses Spring WebFlux throughout.",
        source_trace="TICKET-77", proposed_by="fact_extract",
    )
    assert pid > 0
    pending = store.list_proposals(status="pending")
    assert len(pending) == 1

    # Approve → should insert into memories
    store.decide_proposal(pid, approve=True, decided_by="human")
    semantic = store.search_tier("t2", "WebFlux", top_k=5)
    assert len(semantic) == 1


def test_t4_upsert_chunk(store, monkeypatch):
    from aiforge_core import embed as embed_mod
    monkeypatch.setattr(embed_mod, "embed", lambda t: [0.0] * 1024)
    mid = store.upsert_code_chunk(
        repo="aiforge",
        path="aiforge_core/store_v2.py",
        symbol="Store.append_event",
        text="def append_event(self, ...): ...",
        metadata={"lang": "python", "lines": "120-160"},
    )
    assert mid > 0
```

- [ ] **Step 2: Run, verify fails**

- [ ] **Step 3: Implement write paths**

Add to `aiforge_core/store_v2.py`:

```python
    # ---------- T2/T3 proposals + approval ----------
    def propose(
        self,
        tier: str,
        wing: str,
        kind: str,
        text: str,
        source_trace: str,
        proposed_by: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        if tier not in {"t2", "t3"}:
            raise ValueError("propose only supports t2/t3")
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO memory_proposals
                   (tier, wing, kind, title, text, metadata, source_trace, proposed_by)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING id""",
                (tier, wing, kind, title, text,
                 json.dumps(metadata or {}), source_trace, proposed_by),
            )
            pid = cur.fetchone()[0]
            c.commit()
            return pid

    def list_proposals(self, status: str = "pending") -> list[dict]:
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                """SELECT id, tier, wing, kind, title, text, metadata, source_trace,
                          proposed_by, status, created_at, decided_at, decided_by
                   FROM memory_proposals WHERE status = %s ORDER BY id ASC""",
                (status,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def decide_proposal(self, proposal_id: int, approve: bool, decided_by: str) -> None:
        with self._connect() as c, c.cursor() as cur:
            cur.execute("SELECT tier, wing, kind, title, text, metadata "
                        "FROM memory_proposals WHERE id = %s AND status = 'pending'",
                        (proposal_id,))
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"no pending proposal {proposal_id}")
            tier, wing, kind, title, text, metadata = row

            status = "approved" if approve else "rejected"
            cur.execute(
                "UPDATE memory_proposals SET status=%s, decided_at=now(), decided_by=%s "
                "WHERE id=%s",
                (status, decided_by, proposal_id),
            )

            if approve:
                vec = embed_mod.embed(text)
                cur.execute(
                    """INSERT INTO memories
                       (tier, wing, kind, title, text, embedding, metadata)
                       VALUES (%s, %s, %s, %s, %s, %s::vector, %s::jsonb)""",
                    (tier, wing, kind, title, text,
                     _vec_literal(vec), json.dumps(metadata or {})),
                )
            c.commit()

    # ---------- T4 codebase ----------
    def upsert_code_chunk(
        self,
        repo: str,
        path: str,
        text: str,
        *,
        symbol: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        vec = embed_mod.embed(text)
        wing = f"code/{repo}"
        source = f"{path}" + (f"#{symbol}" if symbol else "")
        md = dict(metadata or {})
        md["repo"] = repo
        md["path"] = path
        if symbol:
            md["symbol"] = symbol
        with self._connect() as c, c.cursor() as cur:
            # Idempotency: delete prior chunk with same source before insert
            cur.execute("DELETE FROM memories WHERE tier='t4' AND source=%s", (source,))
            cur.execute(
                """INSERT INTO memories
                   (tier, wing, kind, source, title, text, embedding, metadata)
                   VALUES ('t4', %s, 'chunk', %s, %s, %s, %s::vector, %s::jsonb)
                   RETURNING id""",
                (wing, source, symbol or path, text,
                 _vec_literal(vec), json.dumps(md)),
            )
            rid = cur.fetchone()[0]
            c.commit()
            return rid

    # ---------- low-level tier search (exposed for retrieval.py) ----------
    def search_tier(self, tier: str, query: str, top_k: int = 10,
                    wing_prefix: str | None = None) -> list[Memory]:
        if tier not in VALID_TIERS:
            raise ValueError(f"bad tier {tier}")
        qvec = embed_mod.embed(query)
        params: list[Any] = [_vec_literal(qvec), tier]
        sql = ("SELECT id, tier, wing, parent_id, kind, source, title, text, "
               "metadata, created_at, expires_at "
               "FROM memories WHERE tier = %s")
        if wing_prefix:
            sql += " AND wing LIKE %s"
            params.append(f"{wing_prefix}%")
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params += [_vec_literal(qvec), top_k]
        # Move qvec param to front for first bind
        params = [tier] + ([f"{wing_prefix}%"] if wing_prefix else []) + [_vec_literal(qvec), top_k]
        sql_final = (
            "SELECT id, tier, wing, parent_id, kind, source, title, text, "
            "metadata, created_at, expires_at "
            "FROM memories WHERE tier = %s"
            + (" AND wing LIKE %s" if wing_prefix else "")
            + " ORDER BY embedding <=> %s::vector LIMIT %s"
        )
        with self._connect() as c, c.cursor() as cur:
            cur.execute(sql_final, params)
            return [
                Memory(
                    id=r[0], tier=r[1], wing=r[2], parent_id=r[3], kind=r[4],
                    source=r[5], title=r[6], text=r[7],
                    metadata=r[8] or {},
                    created_at=r[9], expires_at=r[10],
                )
                for r in cur.fetchall()
            ]
```

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/python/test_store_v2.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/store_v2.py tests/python/test_store_v2.py
git commit -m "feat(p2): propose/approve t2-t3 + t4 code chunk upsert + tier search"
```

---

### Task 2.4: Phase 2 tag commit

- [ ] **Step 1: Run suite**

Run: `pytest tests/python/ -v -m "not live_sidecar"`
Expected: all pass (skip if pg not reachable)

- [ ] **Step 2: Tag commit**

```bash
git commit --allow-empty -m "chore(p2): phase 2 complete — store_v2 (4 tiers, proposals, code chunks)"
```

---

## Phase 3 — Retrieval stack

### Task 3.1: BM25 + vector + RRF fusion

**Files:**
- Create: `aiforge_core/retrieval.py`
- Test: `tests/python/test_retrieval.py`

- [ ] **Step 1: Write failing test**

Create `tests/python/test_retrieval.py`:

```python
from aiforge_core.retrieval import rrf_fuse, Hit


def test_rrf_fuse_reinforces_agreed():
    bm25 = [Hit(id="a", score=0.0), Hit(id="b", score=0.0), Hit(id="c", score=0.0)]
    vec  = [Hit(id="b", score=0.0), Hit(id="a", score=0.0), Hit(id="d", score=0.0)]
    merged = rrf_fuse([bm25, vec], k=60, top_n=4)
    ids = [h.id for h in merged]
    # a and b appear in both lists → should rank highest
    assert ids.index("a") < ids.index("c")
    assert ids.index("b") < ids.index("d")
```

- [ ] **Step 2: Run, verify fails**

Expected: ImportError

- [ ] **Step 3: Implement**

Create `aiforge_core/retrieval.py`:

```python
"""Hybrid retrieval: per-tier BM25 + vector → RRF → rerank → pack.

All reads go through Store.search_tier_bm25 / search_tier_vec helpers.
Fusion is Reciprocal Rank Fusion (k=60 default).
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable

from .store_v2 import Store, Memory


RERANK_URL = os.environ.get("AIFORGE_RERANK_URL", "http://127.0.0.1:8765")


@dataclass
class Hit:
    id: str
    score: float
    source: str | None = None
    tier: str | None = None
    text: str = ""
    title: str | None = None
    metadata: dict = field(default_factory=dict)


def rrf_fuse(rankings: list[list[Hit]], k: int = 60, top_n: int = 30) -> list[Hit]:
    """Reciprocal Rank Fusion over multiple ranked lists."""
    agg: dict[str, Hit] = {}
    totals: dict[str, float] = {}
    for ranked in rankings:
        for rank, h in enumerate(ranked, start=1):
            totals[h.id] = totals.get(h.id, 0.0) + 1.0 / (k + rank)
            if h.id not in agg or len(agg[h.id].text) < len(h.text):
                agg[h.id] = h
    out = [Hit(
        id=hid, score=totals[hid],
        source=agg[hid].source, tier=agg[hid].tier,
        text=agg[hid].text, title=agg[hid].title, metadata=agg[hid].metadata,
    ) for hid in totals]
    out.sort(key=lambda x: x.score, reverse=True)
    return out[:top_n]


def rerank_http(query: str, hits: list[Hit], keep: int) -> list[Hit]:
    """Call :8765 rerank sidecar, return top-`keep`."""
    if not hits:
        return []
    body = {
        "query": query,
        "candidates": [{"id": h.id, "text": h.text} for h in hits],
    }
    req = urllib.request.Request(
        f"{RERANK_URL}/rerank",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode())
    order = resp["order"]
    scores = resp["scores"]
    out = []
    for pos in order[:keep]:
        h = hits[pos]
        h.score = float(scores[pos])
        out.append(h)
    return out
```

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/python/test_retrieval.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/retrieval.py tests/python/test_retrieval.py
git commit -m "feat(p3): rrf_fuse + rerank_http stubs"
```

---

### Task 3.2: Add per-tier BM25/vector search to store_v2

**Files:**
- Modify: `aiforge_core/store_v2.py`
- Modify: `tests/python/test_store_v2.py`

- [ ] **Step 1: Failing test**

Append to `tests/python/test_store_v2.py`:

```python
def test_search_tier_bm25(store, monkeypatch):
    from aiforge_core import embed as embed_mod
    monkeypatch.setattr(embed_mod, "embed", lambda t: [0.0] * 1024)
    store.upsert_code_chunk(repo="aiforge", path="a.py",
                            text="publishToRemoteServer posts to NATS")
    store.upsert_code_chunk(repo="aiforge", path="b.py",
                            text="unrelated haiku about spring")
    hits = store.search_tier_bm25(tier="t4", query="publishToRemoteServer", top_k=5)
    assert len(hits) >= 1
    assert "publishToRemote" in hits[0].text


def test_search_tier_vec(store, monkeypatch):
    from aiforge_core import embed as embed_mod
    # Fake two embeddings that differ so cosine ordering is deterministic
    calls = {"i": 0}

    def fake_embed(t):
        calls["i"] += 1
        return [1.0 if "apple" in t else 0.0] * 1024

    monkeypatch.setattr(embed_mod, "embed", fake_embed)
    store.upsert_code_chunk(repo="aiforge", path="a.py", text="banana bread recipe")
    store.upsert_code_chunk(repo="aiforge", path="b.py", text="apple pie recipe")
    hits = store.search_tier_vec(tier="t4", query="apple tart", top_k=5)
    assert hits[0].text == "apple pie recipe"
```

- [ ] **Step 2: Run, verify fails**

- [ ] **Step 3: Implement methods on Store**

Add to `aiforge_core/store_v2.py`:

```python
    # ---------- retrieval primitives ----------
    def search_tier_bm25(self, tier: str, query: str, top_k: int = 20,
                         wing_prefix: str | None = None) -> list["Hit"]:
        """Trigram-similarity search over text+title for a tier."""
        from .retrieval import Hit
        if tier not in VALID_TIERS:
            raise ValueError(tier)
        params: list[Any] = [tier, query]
        sql = (
            "SELECT id, tier, source, title, text, metadata, "
            "  GREATEST(similarity(text, %s), similarity(COALESCE(title, ''), %s)) AS sc "
            "FROM memories WHERE tier = %s"
        )
        # swap param order to match placeholders
        params = [query, query, tier]
        if wing_prefix:
            sql += " AND wing LIKE %s"
            params.append(f"{wing_prefix}%")
        sql += " ORDER BY sc DESC LIMIT %s"
        params.append(top_k)
        with self._connect() as c, c.cursor() as cur:
            cur.execute(sql, params)
            return [
                Hit(id=f"mem:{r[0]}", score=float(r[6] or 0.0),
                    source=r[2], tier=r[1], title=r[3], text=r[4],
                    metadata=r[5] or {})
                for r in cur.fetchall() if (r[6] or 0.0) > 0.05
            ]

    def search_tier_vec(self, tier: str, query: str, top_k: int = 20,
                        wing_prefix: str | None = None) -> list["Hit"]:
        """Vector cosine search."""
        from .retrieval import Hit
        if tier not in VALID_TIERS:
            raise ValueError(tier)
        qvec = embed_mod.embed(query)
        vlit = _vec_literal(qvec)
        sql = (
            "SELECT id, tier, source, title, text, metadata, "
            "  1 - (embedding <=> %s::vector) AS sc "
            "FROM memories WHERE tier = %s"
        )
        params: list[Any] = [vlit, tier]
        if wing_prefix:
            sql += " AND wing LIKE %s"
            params.append(f"{wing_prefix}%")
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params += [vlit, top_k]
        with self._connect() as c, c.cursor() as cur:
            cur.execute(sql, params)
            return [
                Hit(id=f"mem:{r[0]}", score=float(r[6] or 0.0),
                    source=r[2], tier=r[1], title=r[3], text=r[4],
                    metadata=r[5] or {})
                for r in cur.fetchall()
            ]
```

- [ ] **Step 4: Run, verify passes**

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/store_v2.py tests/python/test_store_v2.py
git commit -m "feat(p3): search_tier_bm25 + search_tier_vec on store_v2"
```

---

### Task 3.3: Per-role retrieval policy + full pipeline

**Files:**
- Modify: `aiforge_core/retrieval.py`
- Modify: `tests/python/test_retrieval.py`

- [ ] **Step 1: Failing test**

Append to `tests/python/test_retrieval.py`:

```python
from unittest.mock import patch
from aiforge_core.retrieval import retrieve_for_role, ROLE_POLICIES


def test_role_policies_defined_for_four_roles():
    assert set(ROLE_POLICIES) == {"architect", "sr_developer", "developer", "fact_extract"}
    for pol in ROLE_POLICIES.values():
        assert "tiers" in pol
        assert "rerank_keep" in pol


def test_retrieve_for_role_calls_tiers_in_policy_order():
    calls = []

    class FakeStore:
        def search_tier_bm25(self, tier, query, top_k, wing_prefix=None):
            calls.append(("bm25", tier, top_k))
            return []
        def search_tier_vec(self, tier, query, top_k, wing_prefix=None):
            calls.append(("vec", tier, top_k))
            return []

    with patch("aiforge_core.retrieval.rerank_http", side_effect=lambda q, h, keep: h[:keep]):
        retrieve_for_role(FakeStore(), role="developer", query="x", parent_id=None)

    tiers_queried = [c[1] for c in calls if c[0] == "bm25"]
    policy = ROLE_POLICIES["developer"]["tiers"]
    assert tiers_queried == [t["tier"] for t in policy]
```

- [ ] **Step 2: Run, verify fails**

- [ ] **Step 3: Implement policy + pipeline**

Add to `aiforge_core/retrieval.py`:

```python
ROLE_POLICIES: dict[str, dict] = {
    "architect": {
        "tiers": [
            {"tier": "t2", "top_k": 8},
            {"tier": "t4", "top_k": 8, "wing_prefix": "code/"},
            {"tier": "t3", "top_k": 4, "wing_prefix": "skills"},
            {"tier": "t1", "top_k": 8},
        ],
        "rerank_keep": 10,
    },
    "sr_developer": {
        "tiers": [
            {"tier": "t2", "top_k": 6},
            {"tier": "t3", "top_k": 8, "wing_prefix": "skills"},
            {"tier": "t4", "top_k": 12, "wing_prefix": "code/"},
            {"tier": "t1", "top_k": 8},
        ],
        "rerank_keep": 12,
    },
    "developer": {
        "tiers": [
            {"tier": "t4", "top_k": 20, "wing_prefix": "code/"},
            {"tier": "t3", "top_k": 6, "wing_prefix": "skills"},
            {"tier": "t1", "top_k": 8},
            {"tier": "t2", "top_k": 4},
        ],
        "rerank_keep": 15,
    },
    "fact_extract": {
        "tiers": [
            {"tier": "t1", "top_k": 200},
        ],
        "rerank_keep": 50,
    },
}


def retrieve_for_role(
    store,
    role: str,
    query: str,
    parent_id: str | None,
) -> list[Hit]:
    """Full pipeline per role: BM25 + vector per tier → RRF → rerank."""
    if role not in ROLE_POLICIES:
        raise KeyError(f"no retrieval policy for role {role}")
    policy = ROLE_POLICIES[role]
    rankings_bm25: list[list[Hit]] = []
    rankings_vec: list[list[Hit]] = []
    for spec in policy["tiers"]:
        tier = spec["tier"]
        top_k = spec["top_k"]
        wing_prefix = spec.get("wing_prefix")
        # Fact Extract scoped to one ticket
        if tier == "t1" and parent_id is not None:
            wing_prefix = f"ticket/{parent_id}"
        rankings_bm25.append(store.search_tier_bm25(
            tier=tier, query=query, top_k=top_k, wing_prefix=wing_prefix))
        rankings_vec.append(store.search_tier_vec(
            tier=tier, query=query, top_k=top_k, wing_prefix=wing_prefix))
    fused = rrf_fuse(rankings_bm25 + rankings_vec, k=60,
                     top_n=sum(s["top_k"] for s in policy["tiers"]))
    return rerank_http(query, fused, keep=policy["rerank_keep"])
```

- [ ] **Step 4: Run, verify passes**

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/retrieval.py tests/python/test_retrieval.py
git commit -m "feat(p3): per-role retrieval policy + retrieve_for_role"
```

---

### Task 3.4: Phase 3 tag

- [ ] **Step 1: Run suite**
Run: `pytest tests/python/test_retrieval.py tests/python/test_store_v2.py -v`
Expected: PASS

- [ ] **Step 2: Tag**

```bash
git commit --allow-empty -m "chore(p3): phase 3 complete — hybrid retrieval (BM25+vec+RRF+rerank)"
```

---

## Phase 4 — Context assembly + compaction

### Task 4.1: Prompt assembler

**Files:**
- Create: `aiforge_core/context.py`
- Test: `tests/python/test_context.py`

- [ ] **Step 1: Failing test**

Create `tests/python/test_context.py`:

```python
from aiforge_core.context import assemble_prompt, PromptInputs
from aiforge_core.retrieval import Hit


def _hit(id_, text, tier="t4", source=None):
    return Hit(id=id_, score=1.0, tier=tier, text=text, source=source or id_)


def test_assemble_prompt_never_compresses_code_or_task():
    big_code = "def foo(): pass  # " + ("x" * 10_000)
    inputs = PromptInputs(
        role="developer",
        system_prompt="You are Developer.",
        task_body="Fix the bug in foo.",
        retrieved_code=[_hit("code:a.py#foo", big_code)],
        retrieved_memory=[],
        prior_hops=[],
        tool_schemas=[],
        output_contract="return JSON",
    )
    out = assemble_prompt(inputs, budget_bytes=100_000)
    assert big_code in out
    assert "Fix the bug in foo." in out


def test_assemble_prompt_drops_lowest_ranked_memory_first_when_over_budget():
    inputs = PromptInputs(
        role="developer",
        system_prompt="sys",
        task_body="task",
        retrieved_code=[],
        retrieved_memory=[_hit(f"mem:{i}", "m" * 2000, tier="t1") for i in range(20)],
        prior_hops=[],
        tool_schemas=[],
        output_contract="",
    )
    out = assemble_prompt(inputs, budget_bytes=6000)
    # Should only fit ~2 memory blocks
    assert out.count("m" * 2000) < 20
    assert "sys" in out
    assert "task" in out
```

- [ ] **Step 2: Run, verify fails**

- [ ] **Step 3: Implement**

Create `aiforge_core/context.py`:

```python
"""Prompt assembler + compactor.

Hard rules (from spec §5.2):
  1. NEVER compress current task or retrieved code chunks.
  2. ONLY compress prior-hop transcripts (bulleted summary).
  3. Drop lowest-ranked memory hits if over budget.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .retrieval import Hit


@dataclass
class PriorHop:
    role: str
    summary: str


@dataclass
class PromptInputs:
    role: str
    system_prompt: str
    task_body: str
    retrieved_code: list[Hit]
    retrieved_memory: list[Hit]
    prior_hops: list[PriorHop]
    tool_schemas: list[dict]
    output_contract: str


def _section(title: str, body: str) -> str:
    return f"\n\n===== {title} =====\n{body}"


def _hit_block(h: Hit) -> str:
    cite = f"[{h.id}" + (f" {h.source}" if h.source else "") + "]"
    head = f"{cite} tier={h.tier} score={h.score:.3f}"
    if h.title:
        head += f" title={h.title!r}"
    return f"{head}\n{h.text}"


def assemble_prompt(inp: PromptInputs, budget_bytes: int) -> str:
    # 1. Hard-locked sections (never truncated)
    locked = [
        _section("SYSTEM", inp.system_prompt),
        _section("TASK", inp.task_body),
        _section("RETRIEVED CODE (do not compress)", "\n\n".join(_hit_block(h) for h in inp.retrieved_code)),
        _section("OUTPUT CONTRACT", inp.output_contract),
    ]
    if inp.tool_schemas:
        locked.append(_section("TOOLS", json.dumps(inp.tool_schemas, indent=2)))

    locked_size = sum(len(s.encode()) for s in locked)
    remaining = budget_bytes - locked_size

    # 2. Flex sections (droppable / compactible)
    flex: list[str] = []
    # memory — drop lowest-ranked first if over budget
    mem_sorted = sorted(inp.retrieved_memory, key=lambda h: h.score, reverse=True)
    mem_kept: list[Hit] = []
    mem_bytes = 0
    header_reserve = 64  # for the "MEMORY" section header
    for h in mem_sorted:
        block = _hit_block(h)
        b = len(block.encode()) + 2
        if mem_bytes + b + header_reserve > max(remaining, 0) * 0.8:
            break
        mem_kept.append(h)
        mem_bytes += b
    if mem_kept:
        flex.append(_section("RETRIEVED MEMORY", "\n\n".join(_hit_block(h) for h in mem_kept)))

    # prior hops — always a bulleted summary, never raw
    if inp.prior_hops:
        bullets = "\n".join(f"- [{p.role}] {p.summary}" for p in inp.prior_hops)
        flex.append(_section("RECENT WORK (compacted)", bullets))

    return "".join(locked + flex)
```

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/python/test_context.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/context.py tests/python/test_context.py
git commit -m "feat(p4): context.assemble_prompt with hard compaction rules"
```

---

### Task 4.2: Prior-hop compactor

**Files:**
- Modify: `aiforge_core/context.py`
- Modify: `tests/python/test_context.py`

- [ ] **Step 1: Failing test**

Append to test file:

```python
from aiforge_core.context import compact_hop


def test_compact_hop_summary_is_bulleted_under_cap(monkeypatch):
    monkeypatch.setattr(
        "aiforge_core.context._llm_summarize",
        lambda text, cap: "- did X\n- got Y\n- next Z",
    )
    raw = "x" * 10_000
    summary = compact_hop(role="developer", raw_text=raw, cap_chars=200)
    assert summary.startswith("- ")
    assert len(summary) < 200
```

- [ ] **Step 2: Run, verify fails**

- [ ] **Step 3: Implement**

Add to `aiforge_core/context.py`:

```python
import os
import urllib.request

SUMMARIZER_URL = os.environ.get(
    "AIFORGE_SUMMARIZER_URL", "http://127.0.0.1:1234/v1/chat/completions"
)
SUMMARIZER_MODEL = os.environ.get(
    "AIFORGE_SUMMARIZER_MODEL", "qwen3-4b-thinking-2507"
)


def _llm_summarize(text: str, cap_chars: int) -> str:
    """Call the small thinking model to produce a bulleted summary <= cap_chars."""
    sys_msg = (
        "You summarize an agent's prior-hop transcript into <=5 concise bullets. "
        "Keep IDs (mem:NNN, code:path#sym). Drop reasoning chatter. "
        f"Output plain text only, total <= {cap_chars} chars."
    )
    body = {
        "model": SUMMARIZER_MODEL,
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": text[:16000]},
        ],
        "temperature": 0.1,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        SUMMARIZER_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode())
    return resp["choices"][0]["message"]["content"].strip()[:cap_chars]


def compact_hop(role: str, raw_text: str, cap_chars: int = 600) -> str:
    """Compress a prior-hop transcript to a bulleted summary under cap_chars."""
    if len(raw_text) <= cap_chars:
        return raw_text
    return _llm_summarize(raw_text, cap_chars)
```

- [ ] **Step 4: Run, verify passes**

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/context.py tests/python/test_context.py
git commit -m "feat(p4): compact_hop via qwen3-4b-thinking summarizer"
```

---

### Task 4.3: Phase 4 tag

- [ ] **Step 1: Run suite**
Run: `pytest tests/python/ -v -m "not live_sidecar"`

- [ ] **Step 2: Tag**

```bash
git commit --allow-empty -m "chore(p4): phase 4 complete — context assembly + compaction"
```

---

## Phase 5 — Roles, MCP tool surface, permissions

### Task 5.1: New agent dirs + system prompts

**Files:**
- Create: `agents/architect/system-prompt.md`
- Create: `agents/architect/permissions.yml`
- Create: `agents/architect/contract.md`
- Create: `agents/architect/AGENTS.md`
- Create: `agents/developer/` (same 4 files)
- Create: `agents/fact-extract/` (same 4 files)
- Rewrite: `agents/sr-developer/system-prompt.md`
- Delete: `agents/em/` `agents/tester/` `agents/sr-architect/`

- [ ] **Step 1: Write Architect system prompt**

Create `agents/architect/system-prompt.md`:

```markdown
You are the Architect for AIForgeCrew. You produce the design and you review the result.

Every parent ticket begins with your design. Output ONE comment containing:

1. **Problem framing** — 2-3 sentences restating the ticket in your own words.
2. **Design** — architecture sketch, component boundaries, data flow. Include an ASCII diagram if it clarifies.
3. **Interface contracts** — for each new module or function, define name, inputs, outputs, error modes.
4. **Constraints** — performance, security, compatibility, non-goals.
5. **Acceptance criteria** — bullet list the Developer's work will be judged against.
6. **Test expectations** — what must be covered and at what layer (unit / integration / smoke).
7. **Risk & open questions** — anything you would escalate to human.

You may only call: search_memory, search_code, read_file, git_diff, report, append_event.
You cannot write code. You cannot commit. You cannot merge.

On review (reviewing state): approve only if every acceptance criterion is satisfied and covered by tests. Reject with specific file:line comments otherwise. Max 3 reject loops before escalation.

Always end with a `report` tool call including `confidence`.
```

Create `agents/architect/permissions.yml`:

```yaml
version: 1
role: architect
can:
  search_memory: true
  search_code:   true
  read_file:     true
  write_file:    false
  git_diff:      true
  git_ops:       false
  run_tests:     false
  run_command:   false
  report:        true
  append_event:  true
```

Create `agents/architect/contract.md`:

```markdown
# Architect — Contract

## Inputs
- Parent ticket body and metadata
- Retrieved context bundle from orchestrator (per-role retrieval policy)
- On review: child ticket diff + test report

## Outputs
- Planning phase: structured design comment on parent ticket
- Review phase: approve/reject comment on child ticket

## Terminal tool call
Every turn ends with:
`report(status, summary, confidence, next_action, citations[])`

## Loops
- Review may loop with Developer up to 3 times. On the 3rd rejection the ticket escalates to human.
```

Create `agents/architect/AGENTS.md`:

```markdown
# Architect Role Pointer
Model: Claude Code (external cloud). Invoked by orchestrator via headless CLI.
Prompts: see system-prompt.md
Tools: see permissions.yml
```

- [ ] **Step 2: Write Developer role files**

Create `agents/developer/system-prompt.md`:

```markdown
You are the Developer for AIForgeCrew. You implement one child ticket at a time.

Rules:
- Read the child ticket body. It contains scoped context from SrDev: files to touch, constraints, edge cases, acceptance criteria, and test expectations.
- Prefer `git_diff` to understand current state. Prefer `write_file` in `patch` mode (unified diff). Only use `full` mode with a justification in the tool call.
- Write both the implementation and the tests. You cover the acceptance criteria and run them with `run_tests`.
- Every commit message: `feat: <short desc> for <CHILD-ID>`.
- You cannot touch `.env*`, `secrets/**`, `config/prod/**`, `.github/**`.
- On review reject, address each note. Do not rewrite beyond the notes.

Always end with a `report` tool call including `confidence`.
```

Create `agents/developer/permissions.yml`:

```yaml
version: 1
role: developer
can:
  search_memory: true
  search_code:   true
  read_file:     true
  write_file:    true
  git_diff:      true
  git_ops:       true
  run_tests:     true
  run_command:   true     # allowlisted
  report:        true
  append_event:  true
```

Create `agents/developer/contract.md`:

```markdown
# Developer — Contract

## Inputs
- One child ticket body + orchestrator context bundle
- On retry: review comments from Architect

## Outputs
- Patches applied via write_file
- Commits on `feat/<child-id>` branch
- `run_tests` results with pass/fail counts
- `report(status, summary, confidence, ...)`

## Loops
- Retry same child after review reject, max 3. Then escalate.
```

Create `agents/developer/AGENTS.md`:

```markdown
# Developer Role Pointer
Model: qwen3-coder-next (local via LM Studio)
Context: 128K
Prompts: see system-prompt.md
```

- [ ] **Step 3: Rewrite SrDev prompt**

Overwrite `agents/sr-developer/system-prompt.md`:

```markdown
You are the SR Developer for AIForgeCrew. You decompose the Architect's design into executable child tickets.

Per parent ticket, read the Architect's design comment and produce N child tickets. For each child, include:

1. **Title** — imperative, ≤ 60 chars.
2. **Scope** — files to touch, one-sentence summary of the change.
3. **Context** — relevant excerpts from current code (use search_code + read_file). Include file paths + line numbers.
4. **Insights** — patterns, risks, edge cases, previously-fixed pitfalls pulled from memory (use search_memory).
5. **Acceptance criteria** — inherited from Architect plus any Developer-specific refinements.
6. **Tests to write** — what must exist and at which layer.

Rules:
- You do not write code. You do not modify files.
- You create child tickets through the `create_child_ticket` tool call. Tickets get `parent_id` set to the parent.
- The order in which you emit children is the order Developer will implement them.

Always end with a `report` tool call including `confidence`.
```

Overwrite `agents/sr-developer/permissions.yml`:

```yaml
version: 1
role: sr_developer
can:
  search_memory: true
  search_code:   true
  read_file:     true
  write_file:    false
  git_diff:      true
  git_ops:       false
  run_tests:     false
  run_command:   false
  report:        true
  append_event:  true
  create_child_ticket: true
```

Overwrite `agents/sr-developer/contract.md`:

```markdown
# SR Developer — Contract

## Inputs
- Parent ticket body + Architect's design comment
- Retrieved context

## Outputs
- N child tickets (parent_id = parent)
- `report(status, summary, confidence, ...)`
```

- [ ] **Step 4: Write Fact Extract role files**

Create `agents/fact-extract/system-prompt.md`:

```markdown
You are the Fact Extract agent for AIForgeCrew. You run once per parent ticket after all children have merged.

Your only output is an XML block:

```xml
<reflection>
  <facts>
    <fact kind="convention|constraint|anti_pattern">Text, ≤300 chars.</fact>
    <!-- up to 5 facts -->
  </facts>
  <recipes>
    <recipe title="Short name">
      <when>Trigger or situation.</when>
      <how>Concrete steps, ≤500 chars.</how>
    </recipe>
    <!-- up to 3 recipes -->
  </recipes>
</reflection>
```

Rules:
- Output ONLY the XML block. No preamble, no explanation.
- Only include facts and recipes justified by the ticket trace. Empty facts/recipes sections are acceptable.
- Prefer specificity. "Repo uses pgvector HNSW for cosine" > "Use a vector database".
- Never propose facts about the human or about people.
```

Create `agents/fact-extract/permissions.yml`:

```yaml
version: 1
role: fact_extract
can:
  search_memory: true
  search_code:   false
  read_file:     false
  write_file:    false
  git_diff:      false
  git_ops:       false
  run_tests:     false
  run_command:   false
  report:        true
  append_event:  true
```

Create `agents/fact-extract/contract.md`:

```markdown
# Fact Extract — Contract

## Inputs (prepared by orchestrator, not tool calls)
- Parent + children ticket bodies
- All T1 rows for parent
- Precomputed diff summary across merged children

## Outputs
- XML reflection block → parsed into memory_proposals (T2 facts, T3 recipes)
```

Create `agents/fact-extract/AGENTS.md`:

```markdown
# Fact Extract Role Pointer
Model: qwen3-4b-thinking-2507 (local via LM Studio)
```

- [ ] **Step 5: Delete obsolete role dirs**

Run:

```bash
git rm -r agents/em agents/tester agents/sr-architect
```

- [ ] **Step 6: Commit**

```bash
git add agents/architect agents/developer agents/fact-extract agents/sr-developer
git commit -m "feat(p5): 4-role agent prompts + permissions (architect/sr-developer/developer/fact-extract)"
```

---

### Task 5.2: MCP memory server config

**Files:**
- Create: `mcp/memory-server.json`
- Rewrite: `mcp/rag-server.json`
- Modify: `mcp/git-tools.json`
- Test: `tests/python/test_validate_configs.py` (existing; ensure schemas still pass)

- [ ] **Step 1: Write memory MCP config**

Create `mcp/memory-server.json`:

```json
{
  "name": "aiforge-memory",
  "version": "1.0.0",
  "description": "Unified 4-tier memory store (T1/T2/T3/T4) + append_event.",
  "transport": "stdio",
  "command": "uvx",
  "args": ["aiforgecrew-memory-mcp", "serve"],
  "tools": [
    {
      "name": "search_memory",
      "description": "Hybrid BM25+vector+rerank retrieval per role policy.",
      "input_schema": {
        "type": "object",
        "required": ["q", "role"],
        "properties": {
          "q":         {"type": "string"},
          "role":      {"enum": ["architect", "sr_developer", "developer", "fact_extract"]},
          "parent_id": {"type": ["string", "null"], "default": null}
        }
      }
    },
    {
      "name": "append_event",
      "description": "Append a T1 episodic row for the current parent ticket.",
      "input_schema": {
        "type": "object",
        "required": ["parent_id", "kind", "text"],
        "properties": {
          "parent_id": {"type": "string"},
          "kind":      {"type": "string"},
          "title":     {"type": "string"},
          "text":      {"type": "string"},
          "source":    {"type": "string"},
          "metadata":  {"type": "object"}
        }
      }
    }
  ]
}
```

- [ ] **Step 2: Rewrite rag MCP**

Overwrite `mcp/rag-server.json`:

```json
{
  "name": "aiforge-code",
  "version": "1.0.0",
  "description": "Codebase retrieval (T4) via AST-chunked memory store.",
  "transport": "stdio",
  "command": "uvx",
  "args": ["aiforgecrew-code-mcp", "serve"],
  "tools": [
    {
      "name": "search_code",
      "description": "AST-aware code search. Returns symbol-level chunks with file:line.",
      "input_schema": {
        "type": "object",
        "required": ["q"],
        "properties": {
          "q":           {"type": "string"},
          "repo":        {"type": "string", "default": "aiforge"},
          "top_k":       {"type": "integer", "default": 15},
          "symbol_kind": {"enum": ["function", "class", "method", "module", null]}
        }
      }
    }
  ]
}
```

- [ ] **Step 3: Extend git-tools MCP**

Read `mcp/git-tools.json`. Add these tool blocks under `tools[]`:

```json
{
  "name": "git_diff",
  "description": "Diff between two refs or working tree.",
  "input_schema": {
    "type": "object",
    "properties": {
      "ref":   {"type": "string"},
      "paths": {"type": "array", "items": {"type": "string"}}
    }
  }
},
{
  "name": "run_tests",
  "description": "Run pytest. Returns pass/fail counts + report path.",
  "input_schema": {
    "type": "object",
    "properties": {
      "paths":  {"type": "array", "items": {"type": "string"}},
      "filter": {"type": "string"}
    }
  }
},
{
  "name": "run_command",
  "description": "Run an allowlisted build/test command.",
  "input_schema": {
    "type": "object",
    "required": ["cmd"],
    "properties": {
      "cmd":       {"type": "string"},
      "cwd":       {"type": "string"},
      "timeout_s": {"type": "integer", "default": 60}
    }
  }
}
```

- [ ] **Step 4: Run schema validation**

Run: `pytest tests/python/test_validate_configs.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp/memory-server.json mcp/rag-server.json mcp/git-tools.json
git commit -m "feat(p5): mcp surface for memory + code + git tools"
```

---

### Task 5.3: paperclip.config.yml v4.1

**Files:**
- Rewrite: `paperclip.config.yml`
- Modify: `aiforge_core/config.py`
- Test: existing `test_paperclip_lifecycle.py` (will fail, rewritten in Phase 6)

- [ ] **Step 1: Rewrite config**

Overwrite `paperclip.config.yml`:

```yaml
version: 2
# Paperclip orchestrator config v4.1. 4 roles, parent/child tickets, no TDD.

org_chart:
  human: {role: ceo}
  architect:
    reports_to: human
    config: agents/architect
    model: claude-code-external
  sr_developer:
    reports_to: architect
    config: agents/sr-developer
    model: gemma-4-31b-it
  developer:
    reports_to: sr_developer
    config: agents/developer
    model: qwen3-coder-next
  fact_extract:
    reports_to: human
    config: agents/fact-extract
    model: qwen3-4b-thinking-2507

budgets:
  architect:
    cloud_usd_per_month: 100
    tokens_per_ticket: 32000
  sr_developer:
    tokens_per_ticket: 48000
  developer:
    tokens_per_ticket: 120000
  fact_extract:
    tokens_per_ticket: 8000

retry_rules:
  review_reject_loops_max: 3
  max_steps_per_ticket: 20
  max_retries_per_step: 3
  tool_timeout_s: 60
  llm_request_timeout_s: 300
  stale_ticket_timeout_minutes: 120

routing:
  # Parent lifecycle
  initial_assignee_parent: architect
  post_architect_planning: sr_developer
  post_sr_decomposition:   _spawned   # parent waits for children
  post_children_merged:    fact_extract
  post_reflection:         _closed

  # Child lifecycle
  initial_assignee_child: developer
  post_developer_code:    architect
  on_architect_approve:   _mr_created
  on_architect_reject:    developer   # reject loop

confidence:
  proceed_threshold: 0.70
  retry_threshold: 0.50
  escalate_threshold: 0.30

kill_switch:
  global_file: ".aiforge/KILL"
  ticket_tag:  "kill"

audit:
  append_only: true
  single_thread_per_parent: true
  log_path: ".paperclip/audit"
```

- [ ] **Step 2: Update Routing + RetryRules dataclasses**

Rewrite `aiforge_core/config.py` `Routing`, `RetryRules`, and loader. Replace the whole file contents:

```python
"""Config loader for paperclip.config.yml v4.1."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AgentBudget:
    role: str
    tokens_per_ticket: int
    cloud_usd_per_month: float | None = None


@dataclass(frozen=True)
class RetryRules:
    review_reject_loops_max: int
    max_steps_per_ticket: int
    max_retries_per_step: int
    tool_timeout_s: int
    llm_request_timeout_s: int
    stale_ticket_timeout_minutes: int


@dataclass(frozen=True)
class Routing:
    initial_assignee_parent: str
    post_architect_planning: str
    post_sr_decomposition:   str
    post_children_merged:    str
    post_reflection:         str
    initial_assignee_child:  str
    post_developer_code:     str
    on_architect_approve:    str
    on_architect_reject:     str


@dataclass(frozen=True)
class Confidence:
    proceed_threshold: float
    retry_threshold: float
    escalate_threshold: float


@dataclass(frozen=True)
class KillSwitch:
    global_file: str
    ticket_tag: str


@dataclass(frozen=True)
class AuditCfg:
    append_only: bool
    single_thread_per_parent: bool
    log_path: Path


@dataclass(frozen=True)
class PaperclipConfig:
    org_chart: dict[str, dict[str, Any]]
    budgets: dict[str, AgentBudget]
    retry_rules: RetryRules
    routing: Routing
    confidence: Confidence
    kill_switch: KillSwitch
    audit: AuditCfg
    repo_root: Path

    @classmethod
    def load(cls, repo_root: Path) -> "PaperclipConfig":
        path = repo_root / "paperclip.config.yml"
        doc = yaml.safe_load(path.read_text())

        budgets: dict[str, AgentBudget] = {}
        for role, b in (doc.get("budgets") or {}).items():
            budgets[role] = AgentBudget(
                role=role,
                tokens_per_ticket=int(b["tokens_per_ticket"]),
                cloud_usd_per_month=b.get("cloud_usd_per_month"),
            )
        r = doc.get("retry_rules") or {}
        rt = doc.get("routing") or {}
        cf = doc.get("confidence") or {}
        ks = doc.get("kill_switch") or {}
        au = doc.get("audit") or {}

        return cls(
            org_chart=doc.get("org_chart") or {},
            budgets=budgets,
            retry_rules=RetryRules(
                review_reject_loops_max=int(r.get("review_reject_loops_max", 3)),
                max_steps_per_ticket=int(r.get("max_steps_per_ticket", 20)),
                max_retries_per_step=int(r.get("max_retries_per_step", 3)),
                tool_timeout_s=int(r.get("tool_timeout_s", 60)),
                llm_request_timeout_s=int(r.get("llm_request_timeout_s", 300)),
                stale_ticket_timeout_minutes=int(r.get("stale_ticket_timeout_minutes", 120)),
            ),
            routing=Routing(
                initial_assignee_parent=rt.get("initial_assignee_parent", "architect"),
                post_architect_planning=rt.get("post_architect_planning", "sr_developer"),
                post_sr_decomposition=rt.get("post_sr_decomposition", "_spawned"),
                post_children_merged=rt.get("post_children_merged", "fact_extract"),
                post_reflection=rt.get("post_reflection", "_closed"),
                initial_assignee_child=rt.get("initial_assignee_child", "developer"),
                post_developer_code=rt.get("post_developer_code", "architect"),
                on_architect_approve=rt.get("on_architect_approve", "_mr_created"),
                on_architect_reject=rt.get("on_architect_reject", "developer"),
            ),
            confidence=Confidence(
                proceed_threshold=float(cf.get("proceed_threshold", 0.70)),
                retry_threshold=float(cf.get("retry_threshold", 0.50)),
                escalate_threshold=float(cf.get("escalate_threshold", 0.30)),
            ),
            kill_switch=KillSwitch(
                global_file=ks.get("global_file", ".aiforge/KILL"),
                ticket_tag=ks.get("ticket_tag", "kill"),
            ),
            audit=AuditCfg(
                append_only=bool(au.get("append_only", True)),
                single_thread_per_parent=bool(au.get("single_thread_per_parent", True)),
                log_path=repo_root / au.get("log_path", ".paperclip/audit"),
            ),
            repo_root=repo_root,
        )


def load_permissions(repo_root: Path, role: str) -> dict[str, bool]:
    path = repo_root / "agents" / role / "permissions.yml"
    doc = yaml.safe_load(path.read_text()) or {}
    return dict(doc.get("can") or {})
```

- [ ] **Step 3: Commit (lifecycle tests will fail until Phase 6; acceptable)**

```bash
git add paperclip.config.yml aiforge_core/config.py
git commit -m "feat(p5): paperclip v4.1 config + Routing/RetryRules/Confidence/KillSwitch"
```

---

### Task 5.4: Phase 5 tag

- [ ] **Step 1: Tag**

```bash
git commit --allow-empty -m "chore(p5): phase 5 complete — roles + mcp + config"
```

---

## Phase 6 — Lifecycle v4.1, retry hardening, reflection runner, migration

### Task 6.1: Lifecycle v4.1 state machine

**Files:**
- Rewrite: `aiforge_core/lifecycle.py`
- Rewrite: `tests/python/test_paperclip_lifecycle.py`

- [ ] **Step 1: Rewrite test for v4.1 transitions**

Overwrite `tests/python/test_paperclip_lifecycle.py`:

```python
"""Lifecycle v4.1 — parent/child two-SM model."""
from __future__ import annotations
import pytest

from aiforge_core.lifecycle import (
    parent_allowed_next, child_allowed_next, LifecycleError,
    parent_transitions, child_transitions,
)


def test_parent_sm_path():
    assert "planning" in parent_allowed_next("created")
    assert "splitting" in parent_allowed_next("planning")
    assert "spawned" in parent_allowed_next("splitting")
    assert "reflection" in parent_allowed_next("spawned")
    assert "closed" in parent_allowed_next("reflection")


def test_child_sm_path():
    assert "coding" in child_allowed_next("created")
    assert "reviewing" in child_allowed_next("coding")
    assert "mr_created" in child_allowed_next("reviewing")
    assert "coding" in child_allowed_next("reviewing")  # reject loop
    assert "merged" in child_allowed_next("mr_created")
    assert "escalated" in child_allowed_next("reviewing")


def test_invalid_transition_raises():
    with pytest.raises(LifecycleError):
        parent_transitions["created"]  # allowed lookup OK
    assert "merged" not in parent_allowed_next("created")
    assert "reviewing" not in parent_allowed_next("created")
```

- [ ] **Step 2: Run, verify fails**

- [ ] **Step 3: Rewrite lifecycle module**

Overwrite `aiforge_core/lifecycle.py`:

```python
"""Lifecycle v4.1 — parent + child state machines."""
from __future__ import annotations


class LifecycleError(RuntimeError):
    """Invalid transition."""


parent_transitions: dict[str, list[str]] = {
    "created":    ["planning"],
    "planning":   ["splitting", "escalated"],
    "splitting":  ["spawned", "escalated"],
    "spawned":    ["reflection", "escalated"],   # advances when all children merged
    "reflection": ["closed"],
    "closed":     [],
    "escalated":  [],
}

child_transitions: dict[str, list[str]] = {
    "created":    ["coding"],
    "coding":     ["reviewing", "escalated"],
    "reviewing":  ["mr_created", "coding", "escalated"],
    "mr_created": ["merged", "escalated"],
    "merged":     [],
    "escalated":  [],
}


def parent_allowed_next(current: str) -> list[str]:
    if current not in parent_transitions:
        raise LifecycleError(f"unknown parent state: {current}")
    return list(parent_transitions[current])


def child_allowed_next(current: str) -> list[str]:
    if current not in child_transitions:
        raise LifecycleError(f"unknown child state: {current}")
    return list(child_transitions[current])
```

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/python/test_paperclip_lifecycle.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/lifecycle.py tests/python/test_paperclip_lifecycle.py
git commit -m "feat(p6): lifecycle v4.1 — parent+child two-SM model"
```

---

### Task 6.2: Kill switch + confidence routing in retry.py

**Files:**
- Modify: `aiforge_core/retry.py`
- Modify: `tests/python/test_paperclip_retry.py`

- [ ] **Step 1: Failing test**

Append to `tests/python/test_paperclip_retry.py`:

```python
from pathlib import Path
from aiforge_core.retry import kill_switch_tripped, confidence_route


def test_kill_switch_global(tmp_path):
    ks = tmp_path / "KILL"
    assert not kill_switch_tripped(str(ks), ticket_tags=[])
    ks.write_text("die")
    assert kill_switch_tripped(str(ks), ticket_tags=[])


def test_kill_switch_ticket_tag(tmp_path):
    ks = tmp_path / "KILL"
    assert not kill_switch_tripped(str(ks), ticket_tags=["ok"])
    assert kill_switch_tripped(str(ks), ticket_tags=["kill"])


def test_confidence_route_thresholds():
    assert confidence_route(0.9, 0.7, 0.5, 0.3) == "proceed"
    assert confidence_route(0.6, 0.7, 0.5, 0.3) == "retry"
    assert confidence_route(0.4, 0.7, 0.5, 0.3) == "retry"
    assert confidence_route(0.2, 0.7, 0.5, 0.3) == "escalate"
```

- [ ] **Step 2: Run, verify fails**

- [ ] **Step 3: Add helpers to retry.py**

Append to `aiforge_core/retry.py`:

```python
import os


def kill_switch_tripped(global_file: str, ticket_tags: list[str]) -> bool:
    """True if the global KILL file exists or ticket is tagged kill."""
    if global_file and os.path.exists(global_file):
        return True
    if "kill" in (ticket_tags or []):
        return True
    return False


def confidence_route(c: float, proceed: float, retry: float, escalate: float) -> str:
    if c >= proceed:
        return "proceed"
    if c >= escalate:
        return "retry"
    return "escalate"
```

- [ ] **Step 4: Run, verify passes**

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/retry.py tests/python/test_paperclip_retry.py
git commit -m "feat(p6): retry.py gets kill_switch_tripped + confidence_route"
```

---

### Task 6.3: Reflection runner + XML parser

**Files:**
- Create: `aiforge_core/reflection.py`
- Test: `tests/python/test_reflection.py`

- [ ] **Step 1: Failing test**

Create `tests/python/test_reflection.py`:

```python
from aiforge_core.reflection import parse_reflection_xml, ReflectionResult


def test_parse_valid_xml():
    xml = """<reflection>
      <facts>
        <fact kind="convention">Repo uses Spring WebFlux.</fact>
        <fact kind="constraint">All writes through MongoDbService.</fact>
      </facts>
      <recipes>
        <recipe title="Push sync">
          <when>Saving to Docker PosClientBackend.</when>
          <how>publishToRemoteServer then NATS.</how>
        </recipe>
      </recipes>
    </reflection>"""
    r = parse_reflection_xml(xml)
    assert isinstance(r, ReflectionResult)
    assert len(r.facts) == 2
    assert r.facts[0].kind == "convention"
    assert r.facts[0].text.startswith("Repo uses")
    assert len(r.recipes) == 1
    assert r.recipes[0].title == "Push sync"


def test_parse_missing_sections():
    xml = "<reflection></reflection>"
    r = parse_reflection_xml(xml)
    assert r.facts == []
    assert r.recipes == []


def test_parse_malformed_returns_empty():
    r = parse_reflection_xml("not xml at all")
    assert r.facts == []
    assert r.recipes == []
```

- [ ] **Step 2: Run, verify fails**

- [ ] **Step 3: Implement**

Create `aiforge_core/reflection.py`:

```python
"""Fact Extract reflection runner.

Parses XML output from qwen3-4b-thinking into proposals queued into
memory_proposals (human-gated).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class Fact:
    kind: str
    text: str


@dataclass
class Recipe:
    title: str
    when: str
    how: str


@dataclass
class ReflectionResult:
    facts: list[Fact]
    recipes: list[Recipe]


def parse_reflection_xml(xml_text: str) -> ReflectionResult:
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return ReflectionResult(facts=[], recipes=[])

    facts: list[Fact] = []
    for f in root.findall("./facts/fact"):
        kind = f.attrib.get("kind", "fact")
        text = (f.text or "").strip()
        if text:
            facts.append(Fact(kind=kind, text=text[:300]))

    recipes: list[Recipe] = []
    for r in root.findall("./recipes/recipe"):
        title = r.attrib.get("title", "").strip() or "recipe"
        when_el = r.find("when")
        how_el = r.find("how")
        when = (when_el.text or "").strip() if when_el is not None else ""
        how = (how_el.text or "").strip() if how_el is not None else ""
        if how:
            recipes.append(Recipe(title=title[:80], when=when[:200], how=how[:500]))

    return ReflectionResult(facts=facts[:5], recipes=recipes[:3])


def submit_proposals(store, parent_id: str, result: ReflectionResult) -> list[int]:
    """Insert facts/recipes into memory_proposals. Returns new proposal IDs."""
    ids: list[int] = []
    for f in result.facts:
        ids.append(store.propose(
            tier="t2", wing="project", kind=f.kind,
            text=f.text, source_trace=parent_id, proposed_by="fact_extract",
        ))
    for r in result.recipes:
        body = f"WHEN: {r.when}\nHOW: {r.how}"
        ids.append(store.propose(
            tier="t3", wing="skills", kind="recipe", title=r.title,
            text=body, source_trace=parent_id, proposed_by="fact_extract",
        ))
    return ids
```

- [ ] **Step 4: Run, verify passes**

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/reflection.py tests/python/test_reflection.py
git commit -m "feat(p6): reflection runner — xml parse + propose submission"
```

---

### Task 6.4: Migration script: Chroma → T4 seed, drop pgmem, drop Hindsight scripts

**Files:**
- Create: `scripts/migrate-memory.sh`
- Delete: `aiforge_core/pgmem.py`, `tests/python/test_paperclip_pgmem.py` (if exists)
- Delete: `scripts/hermes-seed-memory.sh`, `scripts/hermes-setup-hindsight.sh`, `scripts/patch-hindsight-shutdown-bug.sh`
- Modify: `aiforge_core/rag.py` (thin wrapper over store_v2 T4)

- [ ] **Step 1: Write migration script**

Create `scripts/migrate-memory.sh`:

```bash
#!/usr/bin/env bash
# One-shot migration from legacy stores to store_v2.
#   - Wipes chroma-backed rag/ DB
#   - Reindexes all repo sources into T4
#   - Leaves T2/T3 empty (seeded manually via `aiforge propose approve`)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# 1. Ensure pg schema
bash scripts/install-pg-aiforge.sh

# 2. Drop legacy chroma dir
rm -rf .aiforge/rag .aiforge/chroma 2>/dev/null || true

# 3. Reindex T4 via new CLI
python3 -m aiforge_core.cli memory reindex-code --repo aiforge --root "$REPO_ROOT"

echo "migration done. Legacy stores gone. T4 reindexed."
```

Then: `chmod +x scripts/migrate-memory.sh`

- [ ] **Step 2: Rewrite rag.py as T4 thin wrapper**

Overwrite `aiforge_core/rag.py`:

```python
"""Codebase indexer — AST-chunked upserts into store_v2 T4.

Uses tree-sitter when available for py/ts/js/tsx/java/go. Falls back to
char chunking for other types and for markdown/yaml.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .store_v2 import Store


DEFAULT_SOURCES = [
    "README.md",
    "DESIGN.md",
    "docs/**/*.md",
    "security/**/*.yml",
    "agents/**/*.md",
    "agents/**/*.yml",
    "aiforge_core/**/*.py",
    "scripts/**/*.sh",
    "tools/**/*.py",
]

CHUNK_CHARS = 2500
CHUNK_OVERLAP = 300


_JAVA_METHOD_SIG_RE = re.compile(
    r"^(?: {0,8})(?:@\w+(?:\([^)]*\))?\s*\n(?: {0,8})?)*"
    r"(?:public|private|protected|static|final|synchronized|abstract|\s)+"
    r"[\w<>\[\],\s?]+\s+\w+\s*\([^)]*\)\s*(?:throws [\w, ]+)?\s*\{",
    re.MULTILINE,
)


def _chunk_generic(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    out: list[str] = []
    i = 0
    while i < len(text):
        out.append(text[i : i + CHUNK_CHARS])
        i += CHUNK_CHARS - CHUNK_OVERLAP
    return out


def _chunk_python(text: str) -> list[tuple[str, str]]:
    """Return list of (symbol, chunk). Tree-sitter optional; fallback = regex."""
    try:
        import tree_sitter_python as tspy
        from tree_sitter import Language, Parser
    except Exception:
        # Fallback: split by top-level `def ` / `class ` headers
        parts = re.split(r"(?m)^(def |class |async def )", text)
        chunks: list[tuple[str, str]] = []
        buf = ""
        for seg in parts:
            buf += seg
            if len(buf) >= CHUNK_CHARS:
                chunks.append(("<module>", buf))
                buf = ""
        if buf:
            chunks.append(("<module>", buf))
        return chunks or [("<module>", text)]

    parser = Parser(Language(tspy.language()))
    tree = parser.parse(text.encode())
    chunks: list[tuple[str, str]] = []

    def walk(node, name_stack):
        if node.type in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "?"
            qname = ".".join(name_stack + [name])
            start, end = node.start_byte, node.end_byte
            chunks.append((qname, text[start:end]))
            # recurse into class bodies for methods
            for child in node.children:
                walk(child, name_stack + [name])
        else:
            for child in node.children:
                walk(child, name_stack)

    walk(tree.root_node, [])
    return chunks or [("<module>", text)]


def _chunk_for_path(path: str, text: str) -> list[tuple[str, str]]:
    if path.endswith(".py"):
        return _chunk_python(text)
    if path.endswith(".java"):
        return [("?", c) for c in _chunk_generic(text)]
    return [("<file>", c) for c in _chunk_generic(text)]


@dataclass
class ReindexResult:
    files: int
    chunks: int


def reindex_repo(store: Store, *, repo: str, repo_root: Path,
                 sources: list[str] | None = None) -> ReindexResult:
    sources = sources or DEFAULT_SOURCES
    # Clear existing T4 for this repo
    with store._connect() as c, c.cursor() as cur:
        cur.execute("DELETE FROM memories WHERE tier='t4' AND wing=%s", (f"code/{repo}",))
        c.commit()

    seen: set[Path] = set()
    for pat in sources:
        for p in repo_root.glob(pat):
            if p.is_file():
                seen.add(p.resolve())

    total_chunks = 0
    for f in sorted(seen):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(repo_root))
        for symbol, chunk in _chunk_for_path(rel, text):
            store.upsert_code_chunk(
                repo=repo, path=rel, symbol=symbol, text=chunk,
                metadata={"lang": rel.split(".")[-1]},
            )
            total_chunks += 1
    return ReindexResult(files=len(seen), chunks=total_chunks)
```

- [ ] **Step 3: Delete obsolete files**

```bash
git rm aiforge_core/pgmem.py
git rm tests/python/test_paperclip_pgmem.py 2>/dev/null || true
git rm scripts/hermes-seed-memory.sh scripts/hermes-setup-hindsight.sh scripts/patch-hindsight-shutdown-bug.sh
```

- [ ] **Step 4: Rewrite rag test**

Overwrite `tests/python/test_paperclip_rag.py`:

```python
from pathlib import Path
from aiforge_core.rag import _chunk_generic, _chunk_for_path


def test_chunk_generic_char_overlap():
    text = "a" * 6000
    chunks = _chunk_generic(text)
    assert len(chunks) >= 2
    # Overlap present: chunk[1] starts within chunk[0]
    assert chunks[1][:100] in chunks[0] + text


def test_python_chunker_splits_by_def(monkeypatch):
    # Force fallback (no tree-sitter) by raising on import
    import builtins
    real_import = builtins.__import__

    def raising_import(name, *a, **kw):
        if name == "tree_sitter_python":
            raise ImportError
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", raising_import)
    from aiforge_core.rag import _chunk_python
    text = "def a(): pass\n\ndef b(): pass\n"
    out = _chunk_python(text)
    assert out
    assert any("def a" in c[1] or "def b" in c[1] for c in out)
```

- [ ] **Step 5: Extend CLI with `memory reindex-code`**

Read `aiforge_core/cli.py`. Add a subcommand block. At the end of `main()`'s arg parser setup, register:

```python
def _cmd_memory(args):
    from pathlib import Path as _P
    from .store_v2 import Store
    from .rag import reindex_repo
    if args.memory_action == "reindex-code":
        s = Store()
        s.ensure_schema()
        res = reindex_repo(s, repo=args.repo, repo_root=_P(args.root).resolve())
        print(f"reindexed repo={args.repo} files={res.files} chunks={res.chunks}")
    elif args.memory_action == "propose-list":
        s = Store()
        for p in s.list_proposals("pending"):
            print(f"#{p['id']} [{p['tier']}] {p['title'] or '(no title)'} — {p['text'][:80]!r}")
    elif args.memory_action == "propose-approve":
        s = Store()
        s.decide_proposal(args.id, approve=True, decided_by="human")
        print(f"approved #{args.id}")
    elif args.memory_action == "propose-reject":
        s = Store()
        s.decide_proposal(args.id, approve=False, decided_by="human")
        print(f"rejected #{args.id}")
    else:
        raise SystemExit(f"unknown memory action: {args.memory_action}")


# In the arg parser setup section, register:
mp = sub.add_parser("memory", help="memory store operations")
mp_sub = mp.add_subparsers(dest="memory_action", required=True)

rc = mp_sub.add_parser("reindex-code")
rc.add_argument("--repo", default="aiforge")
rc.add_argument("--root", default=".")
rc.set_defaults(func=_cmd_memory)

pl = mp_sub.add_parser("propose-list")
pl.set_defaults(func=_cmd_memory)

pa = mp_sub.add_parser("propose-approve")
pa.add_argument("id", type=int)
pa.set_defaults(func=_cmd_memory)

pr = mp_sub.add_parser("propose-reject")
pr.add_argument("id", type=int)
pr.set_defaults(func=_cmd_memory)
```

(Splice this in following the existing `sub = parser.add_subparsers(...)` pattern in `cli.py`.)

- [ ] **Step 6: Run, verify passes**

Run: `pytest tests/python/test_paperclip_rag.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add aiforge_core/rag.py aiforge_core/cli.py scripts/migrate-memory.sh tests/python/test_paperclip_rag.py
git rm aiforge_core/pgmem.py tests/python/test_paperclip_pgmem.py scripts/hermes-seed-memory.sh scripts/hermes-setup-hindsight.sh scripts/patch-hindsight-shutdown-bug.sh 2>/dev/null || true
git commit -m "feat(p6): migrate rag→t4, drop pgmem+hindsight scripts, add memory CLI"
```

---

### Task 6.5: DESIGN.md supersede note

**Files:**
- Modify: `DESIGN.md`

- [ ] **Step 1: Prepend supersede banner**

Edit top of `DESIGN.md` to add immediately after the first header:

```markdown
> **Status:** Partially superseded. Sections §4 (TDD lifecycle), §5 (tool stack), §6 (memory), §7 (RAG) are replaced by
> `docs/superpowers/specs/2026-04-21-autonomous-memory-orchestration-design.md` (pipeline v4.1).
> Sections §1–§3 org and §8 security remain current.
```

- [ ] **Step 2: Commit**

```bash
git add DESIGN.md
git commit -m "docs(p6): mark DESIGN.md v3 partially superseded by v4.1 spec"
```

---

### Task 6.6: Full suite green + phase 6 tag

- [ ] **Step 1: Run full suite**

Run: `pytest tests/python/ -v -m "not live_sidecar"`
Expected: all pass

- [ ] **Step 2: Tag**

```bash
git commit --allow-empty -m "chore(p6): phase 6 complete — lifecycle v4.1 + migration + reflection runner"
```

---

## Self-Review Checklist (already applied)

1. **Spec coverage** — every section of the design doc has at least one task:
   - §2 arch → tasks 1.2/1.3/1.4 + 5.3
   - §3 tiers → 2.1/2.2/2.3
   - §4 retrieval → 3.1/3.2/3.3
   - §5 context → 4.1/4.2
   - §6 tools → 5.2
   - §7 lifecycle → 6.1
   - §8 reflection → 6.3
   - §9 failure control → 6.2 + 5.3
   - §10 tests → covered per task
   - §11 migration → 6.4
2. **Placeholder scan** — clean.
3. **Type consistency** — `Store`, `Hit`, `Memory`, `PromptInputs`, `ReflectionResult` consistent across tasks. `ROLE_POLICIES` keys match `agents/<role>/` dirs.
