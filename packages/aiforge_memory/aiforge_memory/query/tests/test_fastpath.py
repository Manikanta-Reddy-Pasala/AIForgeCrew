"""L6 fastpath — regex matchers for explicit symbols / tickets / files."""
from __future__ import annotations

from aiforge_memory.query import fastpath as fp


def test_ticket_match() -> None:
    h = fp.detect("fix ONE-123 and please look at it")
    assert h is not None
    assert h.kind == "ticket"
    assert h.value == "ONE-123"


def test_symbol_match() -> None:
    h = fp.detect("trace PaymentService.processPayment under load")
    assert h is not None
    assert h.kind == "symbol"
    assert h.value.startswith("PaymentService")


def test_file_match() -> None:
    h = fp.detect("look at api/main.py for clues")
    assert h is not None
    assert h.kind == "file"
    assert h.value == "api/main.py"


def test_no_match_plain_prose() -> None:
    h = fp.detect("explain how the login flow works")
    assert h is None


def test_ticket_beats_symbol() -> None:
    """If both a ticket and symbol-shaped token exist, ticket wins."""
    h = fp.detect("ABC-9 PaymentService.process is broken")
    assert h.kind == "ticket"
