"""QA + QA-failed ticket statuses (added 2026-05-29)."""
from __future__ import annotations

import pytest

from aiforge_core.tickets import store


def test_qa_statuses_present():
    assert "qa" in store.VALID_STATUS
    assert "qa_failed" in store.VALID_STATUS


def test_core_statuses_preserved():
    assert {
        "todo", "in_progress", "in_review",
        "done", "blocked", "cancelled",
    } <= store.VALID_STATUS


def test_update_status_rejects_unknown():
    # Validation happens before any DB access, so this needs no Postgres.
    with pytest.raises(ValueError):
        store.update_status(1, "not_a_status")
