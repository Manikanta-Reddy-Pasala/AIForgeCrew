"""F1: _persist_ticket_media must honour the SQLite (embedded) backend.

Every sibling memory writer branches on ``backend_select.embedded()`` and
writes to ``sqlite_memory`` when no Neo4j is configured. ``_persist_ticket_media``
did not — it always went straight to bolt://…:7687, so on the default embedded
backend the ``kind="attachment"`` observation was never persisted and the Doer
could never recall prior screenshots. These tests pin both branches.
"""
from dataclasses import dataclass, field

from aiforge_core.runtime import adk_runner


@dataclass
class _Ticket:
    identifier: str = "ONE-200"
    project: str = "demo"
    metadata: dict = field(default_factory=lambda: {
        "attached_files": [
            {"name": "shot.png", "path": "/w/shot.png"},
            {"name": "notes.txt", "path": "/w/notes.txt"},
        ],
    })


def test_embedded_writes_attachment_to_sqlite(monkeypatch):
    monkeypatch.setattr(
        "aiforge_core.memory.backend_select.embedded", lambda: True,
    )
    captured = {}

    def _fake_write_unit(**kwargs):
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(
        "aiforge_core.memory.sqlite_memory.write_unit", _fake_write_unit,
    )

    adk_runner._persist_ticket_media(_Ticket())

    assert captured.get("kind") == "attachment"
    assert captured.get("repo") == "demo"
    assert captured.get("ticket") == "ONE-200"
    # only the image was captured, not the .txt
    refs = (captured.get("metadata") or {}).get("media_refs")
    assert refs == ["/w/shot.png"]


