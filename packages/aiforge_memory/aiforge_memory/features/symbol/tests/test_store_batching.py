"""N14 — store writers must batch rows through UNWIND, not issue one
Cypher round trip per row."""
from __future__ import annotations

from aiforge_memory.features.chunk import store as chunk_store
from aiforge_memory.features.chunk.embed import WalkedChunk
from aiforge_memory.features.symbol import store as symbol_store
from aiforge_memory.features.symbol.extract import WalkedFile, WalkedSymbol
from aiforge_memory.features.symbol.extract_calls import CallEdge


class _Counters:
    nodes_deleted = 0


class _Summary:
    counters = _Counters()


class _FakeResult:
    def consume(self):
        return _Summary()


class _FakeSession:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        self._calls.append((cypher, params))
        return _FakeResult()


class _FakeDriver:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def session(self):
        return _FakeSession(self.calls)


def _walked(path: str, n_symbols: int = 3) -> WalkedFile:
    return WalkedFile(
        repo="t", path=path, hash=f"h-{path}", lang="python", lines=10,
        symbols=[WalkedSymbol(fqname=f"{path}::f{i}", kind="function",
                              file_path=path, signature=f"def f{i}(): ...")
                 for i in range(n_symbols)],
        imports=[],
    )


def test_files_and_symbols_use_unwind_batches():
    drv = _FakeDriver()
    walked = [_walked(f"f{i}.py", n_symbols=3) for i in range(10)]
    counts = symbol_store.upsert_files_and_symbols(
        drv, repo="t", walked_files=walked,
    )
    assert counts["files"] == 10
    assert counts["symbols"] == 30
    # 10 files × 3 symbols would have been ≥40 per-row calls before;
    # batched it's one call per statement kind (all under _BATCH rows).
    assert len(drv.calls) <= 5
    for cypher, params in drv.calls:
        assert "UNWIND" in cypher
        assert "rows" in params or "paths" in params
    # Every per-row statement carries a rows list, not scalars.
    upsert_syms = [p for c, p in drv.calls if "Symbol_v2 {repo: $repo, fqname: r.fqname}" in c]
    assert upsert_syms and len(upsert_syms[0]["rows"]) == 30


def test_batches_split_at_500_rows():
    drv = _FakeDriver()
    walked = [_walked(f"f{i}.py", n_symbols=1) for i in range(501)]
    symbol_store.upsert_files_and_symbols(drv, repo="t", walked_files=walked)
    file_batches = [p["rows"] for c, p in drv.calls
                    if "MERGE (f:File_v2" in c and "UNWIND" in c]
    assert [len(b) for b in file_batches] == [500, 1]


def test_call_edges_use_unwind_batches():
    drv = _FakeDriver()
    edges = [CallEdge(repo="t", caller_fqname=f"a.py::f{i}",
                      callee_fqname=f"b.py::g{i}", confidence=0.8)
             for i in range(20)]
    counts = symbol_store.upsert_call_edges(
        drv, repo="t", edges=edges, file_paths=["a.py", "b.py"],
    )
    assert counts["calls"] == 20
    assert len(drv.calls) == 2          # one prune batch + one upsert batch
    assert all("UNWIND" in c for c, _ in drv.calls)


def test_chunks_use_unwind_batches():
    drv = _FakeDriver()
    chunks = [WalkedChunk(id=f"id{i}", repo="t", file_path="a.py",
                          text="x")
              for i in range(20)]
    counts = chunk_store.upsert_chunks(drv, repo="t", chunks=chunks)
    assert counts["chunks"] == 20
    assert len(drv.calls) == 2          # one prune batch + one upsert batch
    assert all("UNWIND" in c for c, _ in drv.calls)
