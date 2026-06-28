"""L1 — CLI dispatch + doctor exit codes.

After the per-command split each subcommand owns its own module under
``aiforge_memory.api.commands``; tests patch where each helper actually
lives now (the package-level ``cli.py`` is just argparse wiring).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from aiforge_memory.api import cli
from aiforge_memory.core import state as sdb


def test_cli_ingest_subcommand_dispatches_to_flow(tmp_path, monkeypatch) -> None:
    """`aiforge-memory ingest <repo>` calls flow.ingest_repo with parsed args."""
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    fake_driver = MagicMock()
    monkeypatch.setenv("AIFORGE_CODEMEM_STATE_DB", str(tmp_path / "state.db"))

    with patch("aiforge_memory.api.commands.ingest.driver",
               return_value=fake_driver), \
         patch("aiforge_memory.api.commands.ingest.flow.ingest_repo") as ingest, \
         patch("aiforge_memory.api.commands.ingest.schema.apply"):
        ingest.return_value = type(
            "R", (), {"status": "indexed", "pack_sha": "sha", "repo": "rX"}
        )
        rc = cli.main(["ingest", "rX", "--path", str(tmp_path)])
    assert rc == 0
    ingest.assert_called_once()


def test_cli_doctor_returns_0_when_all_green() -> None:
    with patch("aiforge_memory.api.commands.doctor._check_repomix",
               return_value=(True, "ok")), \
         patch("aiforge_memory.api.commands.doctor._check_neo4j",
               return_value=(True, "ok")), \
         patch("aiforge_memory.api.commands.doctor._check_llm",
               return_value=(True, "ok")):
        rc = cli.main(["doctor"])
    assert rc == 0


def test_cli_doctor_returns_1_when_repomix_missing() -> None:
    with patch("aiforge_memory.api.commands.doctor._check_repomix",
               return_value=(False, "missing")), \
         patch("aiforge_memory.api.commands.doctor._check_neo4j",
               return_value=(True, "ok")), \
         patch("aiforge_memory.api.commands.doctor._check_llm",
               return_value=(True, "ok")):
        rc = cli.main(["doctor"])
    assert rc == 1
