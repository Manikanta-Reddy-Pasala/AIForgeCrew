"""L1 — orchestrator: pack → summarize → upsert, idempotent."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from aiforge_memory.core import state as sdb
from aiforge_memory.features.flow import runner as flow
from aiforge_memory.features.repo.extract import RepoSummary

FIX = Path(__file__).parent / "fixtures"


def test_flow_first_run_calls_pack_summary_writer(tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    fake_driver = MagicMock()
    fake_pack = ("# pack text", "sha-AAA")
    fake_summary = RepoSummary(lang_primary="python", build_cmd="make build",
                               runbook_md="r" * 600)

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=fake_pack) as p, \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=fake_summary) as s, \
         patch("aiforge_memory.features.flow.runner.repo_writer.upsert_repo") as w:
        result = flow.ingest_repo(
            repo_name="rA", repo_path=str(FIX / "tiny_repo"),
            driver=fake_driver, state_conn=state, skip_services=True,
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

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=fake_pack), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize") as s, \
         patch("aiforge_memory.features.flow.runner.repo_writer.upsert_repo") as w:
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

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=("# pack", "sha-SAME")), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=RepoSummary(runbook_md="r" * 600)) as s, \
         patch("aiforge_memory.features.flow.runner.repo_writer.upsert_repo") as w:
        result = flow.ingest_repo(
            repo_name="rC", repo_path=str(FIX / "tiny_repo"),
            driver=fake_driver, state_conn=state, force=True, skip_services=True,
        )
    assert result.status == "indexed"
    s.assert_called_once()
    w.assert_called_once()
