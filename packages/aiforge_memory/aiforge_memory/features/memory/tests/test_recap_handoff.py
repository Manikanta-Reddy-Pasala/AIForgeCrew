"""Gap #5 — recap + handoff CLI commands.

``recap``   → human-readable digest of a repo's recent memory.
``handoff`` → portable JSON snapshot (active decisions + recent
              observations/notes) to seed a fresh session.

Unit-level: the pure builders are tested directly; dispatch is checked
through ``cli.main`` with the driver + ``list_memory`` patched, so no
live Neo4j is required.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from aiforge_memory.api import cli
from aiforge_memory.api.commands import handoff, recap

_ROWS = [
    {"id": "d1", "label": "Decision_v2", "title": "Use accounting side",
     "text": "payee resolved by accounting side", "kind": "active",
     "created_at": "2026-05-24T15:00:00Z"},
    {"id": "o1", "label": "Observation_v2", "title": "",
     "text": "Bank Charges is an expense ledger, not a bank",
     "kind": "learning", "created_at": "2026-05-24T15:05:00Z"},
    {"id": "n1", "label": "Note_v2", "title": "Migration",
     "text": "reindex needed after v2", "kind": "",
     "created_at": "2026-05-24T15:10:00Z"},
]


def test_render_recap_groups_by_label():
    out = recap._render_recap("Spandana", _ROWS)
    assert "Spandana" in out
    assert "Decision_v2" in out
    assert "Observation_v2" in out
    assert "Bank Charges" in out


def test_render_recap_empty_is_explicit():
    out = recap._render_recap("R", [])
    assert "no memory" in out.lower()


def test_build_handoff_partitions_by_label():
    snap = handoff._build_handoff("Spandana", _ROWS)
    assert snap["repo"] == "Spandana"
    assert snap["count"] == 3
    assert [d["id"] for d in snap["decisions"]] == ["d1"]
    assert [o["id"] for o in snap["observations"]] == ["o1"]
    assert [n["id"] for n in snap["notes"]] == ["n1"]


def test_build_handoff_is_json_serializable():
    snap = handoff._build_handoff("R", _ROWS)
    json.dumps(snap)  # must not raise


def test_cli_recap_dispatches_to_list_memory():
    fake_driver = MagicMock()
    with patch("aiforge_memory.api.commands.recap.driver",
               return_value=fake_driver), \
         patch("aiforge_memory.api.commands.recap.memory_writer.list_memory",
               return_value=_ROWS) as lm:
        rc = cli.main(["recap", "Spandana", "--limit", "10"])
    assert rc == 0
    lm.assert_called_once()
    assert lm.call_args.kwargs["repo"] == "Spandana"
    assert lm.call_args.kwargs["limit"] == 10


def test_cli_handoff_dispatches_and_emits_json(capsys):
    fake_driver = MagicMock()
    with patch("aiforge_memory.api.commands.handoff.driver",
               return_value=fake_driver), \
         patch("aiforge_memory.api.commands.handoff.memory_writer.list_memory",
               return_value=_ROWS):
        rc = cli.main(["handoff", "Spandana"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["repo"] == "Spandana"
    assert payload["count"] == 3
