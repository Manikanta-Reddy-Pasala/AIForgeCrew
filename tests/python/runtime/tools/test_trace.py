from __future__ import annotations

from unittest.mock import patch

from aiforge_core.runtime.tools import _trace


def test_emit_calls_observability_with_label_and_props():
    with patch("aiforge_core.runtime.tools._trace._safe_emit") as mock:
        _trace.emit("Think", {"thought": "hi", "ticket_id": "ONE-1"})
    mock.assert_called_once()
    args, _kwargs = mock.call_args
    assert args[0] == "Think"
    assert args[1]["thought"] == "hi"
    assert args[1]["ticket_id"] == "ONE-1"
    assert "ts" in args[1]


def test_emit_truncates_oversize_strings_to_4kb():
    big = "x" * 5000
    with patch("aiforge_core.runtime.tools._trace._safe_emit") as mock:
        _trace.emit("Think", {"thought": big})
    sent = mock.call_args[0][1]
    assert len(sent["thought"]) <= 4096
    assert sent["thought"].endswith("...[truncated]")


def test_emit_never_raises_on_observability_failure():
    with patch(
        "aiforge_core.runtime.tools._trace._safe_emit",
        side_effect=RuntimeError("neo4j down"),
    ):
        _trace.emit("Think", {"thought": "hi"})  # must not raise
