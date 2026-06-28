"""N5 — full ingest must not re-summarize files that are unchanged AND
already carry a non-empty summary in the graph."""
from __future__ import annotations

from unittest.mock import MagicMock

from aiforge_memory.core import state as sdb
from aiforge_memory.features.flow import runner as flow
from aiforge_memory.features.symbol.extract import WalkedFile, WalkedSymbol


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, summarized_paths):
        self._paths = summarized_paths

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        assert "f.summary" in cypher
        return _FakeResult([{"path": p} for p in self._paths])


class _FakeDriver:
    def __init__(self, summarized_paths):
        self._paths = summarized_paths

    def session(self):
        return _FakeSession(self._paths)


def _walked(path: str, file_hash: str) -> WalkedFile:
    return WalkedFile(
        repo="t", path=path, hash=file_hash, lang="python", lines=3,
        symbols=[WalkedSymbol(fqname=f"{path}::f", kind="function",
                              file_path=path, signature="def f(): ...")],
    )


def test_unchanged_summarized_files_are_skipped(tmp_path):
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    sdb.upsert_file_hashes(state, repo="t",
                           hashes={"a.py": "h-a", "b.py": "old-b"})

    walked = [
        _walked("a.py", "h-a"),    # unchanged + summarized → skip
        _walked("b.py", "h-b"),    # changed                → keep
        _walked("c.py", "h-c"),    # new file               → keep
    ]
    driver = _FakeDriver(summarized_paths=["a.py", "b.py"])
    out = flow._files_needing_summary(
        walked, driver=driver, state_conn=state, repo="t",
    )
    assert [wf.path for wf in out] == ["b.py", "c.py"]


def test_unchanged_but_unsummarized_file_is_kept(tmp_path):
    """Hash match alone is not enough — an empty/missing graph summary
    (e.g. earlier llm_error) must still be retried."""
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    sdb.upsert_file_hashes(state, repo="t", hashes={"a.py": "h-a"})

    walked = [_walked("a.py", "h-a")]
    driver = _FakeDriver(summarized_paths=[])
    out = flow._files_needing_summary(
        walked, driver=driver, state_conn=state, repo="t",
    )
    assert [wf.path for wf in out] == ["a.py"]


def test_cold_start_summarizes_everything(tmp_path):
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    walked = [_walked("a.py", "h-a")]
    out = flow._files_needing_summary(
        walked, driver=_FakeDriver([]), state_conn=state, repo="t",
    )
    assert out == walked


def test_lookup_failure_falls_back_to_all(tmp_path):
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    sdb.upsert_file_hashes(state, repo="t", hashes={"a.py": "h-a"})
    broken = MagicMock()
    broken.session.side_effect = RuntimeError("neo4j down")
    walked = [_walked("a.py", "h-a")]
    out = flow._files_needing_summary(
        walked, driver=broken, state_conn=state, repo="t",
    )
    assert out == walked
