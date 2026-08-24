"""Unit tests for runtime.observability event emission."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.runtime import observability as obs


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIFORGE_OBSERVABILITY_DISABLE", raising=False)


def test_trim_compacts_whitespace_and_caps_length() -> None:
    long = "a" * 1000
    out = obs._trim(f"  hello   world\n\n {long}  ")
    assert "hello world" in out
    assert len(out) <= 600
    assert out.endswith("…")


def test_trim_handles_none_and_empty() -> None:
    assert obs._trim(None) == ""
    assert obs._trim("") == ""


def test_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_OBSERVABILITY_DISABLE", "1")
    assert obs._is_disabled() is True
    # When disabled, make_stage_callbacks returns (None, None)
    before, after = obs.make_stage_callbacks("doer")
    assert before is None
    assert after is None


def test_emit_pr_opened_calls_add_event() -> None:
    fake_add_event = MagicMock()
    from aiforge_core.tickets import store as tickets_mod
    with patch.object(tickets_mod, "add_event", fake_add_event):
        obs.emit_pr_opened(
            ticket_id=42,
            pr_url="https://github.com/x/y/pull/1",
            branch="aiforge/ONE-1",
        )
    fake_add_event.assert_called_once()
    call_args = fake_add_event.call_args
    assert call_args[0][0] == 42                   # ticket_id
    assert call_args[0][1] == "git_pr"             # agent_role
    assert call_args[0][2] == "pr_opened"          # kind
    assert "github.com" in call_args[0][3]         # body has the url
    assert call_args[0][4]["pr_url"].endswith("pull/1")


def test_emit_pr_opened_no_url_skips() -> None:
    fake_add_event = MagicMock()
    from aiforge_core.tickets import store as tickets_mod
    with patch.object(tickets_mod, "add_event", fake_add_event):
        obs.emit_pr_opened(ticket_id=42, pr_url="", branch="")
    fake_add_event.assert_not_called()


def test_emit_commit_calls_add_event() -> None:
    fake_add_event = MagicMock()
    from aiforge_core.tickets import store as tickets_mod
    with patch.object(tickets_mod, "add_event", fake_add_event):
        obs.emit_commit(
            ticket_id=42,
            sha="abc1234567890",
            message="feat(ONE-1): add serial reconciler",
        )
    fake_add_event.assert_called_once()
    call_args = fake_add_event.call_args
    assert call_args[0][2] == "commit"
    assert "abc12345" in call_args[0][3]   # short sha in body
    assert "serial reconciler" in call_args[0][3]


def test_emit_commit_empty_sha_skips() -> None:
    fake_add_event = MagicMock()
    from aiforge_core.tickets import store as tickets_mod
    with patch.object(tickets_mod, "add_event", fake_add_event):
        obs.emit_commit(ticket_id=42, sha="", message="x")
    fake_add_event.assert_not_called()


def test_emit_swallows_postgres_errors() -> None:
    fake_add_event = MagicMock(side_effect=RuntimeError("pg down"))
    from aiforge_core.tickets import store as tickets_mod
    with patch.object(tickets_mod, "add_event", fake_add_event):
        # Must NOT raise — best-effort audit
        obs.emit_pr_opened(ticket_id=42, pr_url="https://x/y/p/1")
    fake_add_event.assert_called_once()


def test_make_stage_callbacks_returns_pair() -> None:
    before, after = obs.make_stage_callbacks("doer")
    assert callable(before)
    assert callable(after)


def test_ticket_id_from_state_handles_missing_identifier() -> None:
    state = SimpleNamespace(get=lambda k: None)
    # Empty / missing identifier returns None gracefully
    assert obs._ticket_id_from_state(state) is None
