from __future__ import annotations

from unittest.mock import patch

from aiforge_core.runtime.tools import cognition as cg


def test_think_returns_ok_and_emits_trace():
    with patch("aiforge_core.runtime.tools.cognition.emit") as mock:
        out = cg.think("considering options")
    assert out == {"ok": True}
    mock.assert_called_once()
    label, props = mock.call_args[0]
    assert label == "Think"
    assert props["thought"] == "considering options"


def test_think_caps_oversize_thought():
    big = "x" * 5000
    with patch("aiforge_core.runtime.tools.cognition.emit") as mock:
        cg.think(big)
    sent = mock.call_args[0][1]["thought"]
    assert len(sent.encode("utf-8")) <= 4096
    assert sent.endswith("...[truncated]")


def test_think_coerces_non_string():
    with patch("aiforge_core.runtime.tools.cognition.emit") as mock:
        out = cg.think(42)
    assert out == {"ok": True}
    sent = mock.call_args[0][1]["thought"]
    assert sent == "42"


def test_finish_doer_ok():
    out = cg.finish("all green", status="done", _agent_role="doer")
    assert out["ok"]
    assert out["terminate"] is True
    assert out["summary"] == "all green"
    assert out["status"] == "done"


def test_finish_non_doer_rejected():
    out = cg.finish("done", _agent_role="planner")
    assert out["ok"] is False
    assert out["error"] == "agent_not_authorized"


def test_finish_invalid_status():
    out = cg.finish("done", status="weird", _agent_role="doer")
    assert out["ok"] is False
    assert out["error"] == "invalid_status"


def test_finish_blocked_status_passes():
    out = cg.finish("stuck on test infra", status="blocked", _agent_role="doer")
    assert out["ok"]
    assert out["status"] == "blocked"


def test_finish_summary_truncated():
    big = "y" * 5000
    out = cg.finish(big, _agent_role="doer")
    assert out["ok"]
    assert len(out["summary"].encode("utf-8")) <= 2048
    assert out["summary"].endswith("...[truncated]")


def test_finish_no_role_assumes_doer():
    out = cg.finish("worked")
    assert out["ok"]
