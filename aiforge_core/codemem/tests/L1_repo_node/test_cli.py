"""L1 — CLI dispatch + doctor exit codes."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from aiforge_core.codemem.api import cli
from aiforge_core.codemem.store import state_db as sdb


def test_cli_ingest_subcommand_dispatches_to_flow(tmp_path, monkeypatch) -> None:
    """`aiforge-codemem ingest <repo>` calls flow.ingest_repo with parsed args."""
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    fake_driver = MagicMock()
    monkeypatch.setenv("AIFORGE_CODEMEM_STATE_DB", str(tmp_path / "state.db"))

    with patch("aiforge_core.codemem.api.cli._driver", return_value=fake_driver), \
         patch("aiforge_core.codemem.api.cli.flow.ingest_repo") as ingest, \
         patch("aiforge_core.codemem.api.cli.schema.apply"):
        ingest.return_value = type(
            "R", (), {"status": "indexed", "pack_sha": "sha", "repo": "rX"}
        )
        rc = cli.main(["ingest", "rX", "--path", str(tmp_path)])
    assert rc == 0
    ingest.assert_called_once()


def test_cli_doctor_returns_0_when_all_green() -> None:
    with patch("aiforge_core.codemem.api.cli._check_repomix", return_value=(True, "ok")), \
         patch("aiforge_core.codemem.api.cli._check_neo4j", return_value=(True, "ok")), \
         patch("aiforge_core.codemem.api.cli._check_llm", return_value=(True, "ok")):
        rc = cli.main(["doctor"])
    assert rc == 0


def test_cli_doctor_returns_1_when_repomix_missing() -> None:
    with patch("aiforge_core.codemem.api.cli._check_repomix",
               return_value=(False, "missing")), \
         patch("aiforge_core.codemem.api.cli._check_neo4j", return_value=(True, "ok")), \
         patch("aiforge_core.codemem.api.cli._check_llm", return_value=(True, "ok")):
        rc = cli.main(["doctor"])
    assert rc == 1
