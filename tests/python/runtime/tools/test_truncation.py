from __future__ import annotations

from aiforge_core.runtime.tools.truncation import mark_truncated


def test_no_truncation_when_under_cap():
    out, was = mark_truncated("hello", 100)
    assert out == "hello"
    assert was is False


def test_truncates_with_marker():
    raw = "x" * 1000
    out, was = mark_truncated(raw, 500)
    assert was is True
    assert "<truncated bytes_dropped=500>" in out
    # Body still capped at 500 bytes + marker
    assert out.startswith("x" * 500)


def test_unicode_safe_truncation():
    raw = "café" * 200
    out, was = mark_truncated(raw, 50)
    assert was is True
    assert "<truncated bytes_dropped=" in out


def test_non_string_passthrough():
    out, was = mark_truncated(b"bytes", 100)
    assert out == b"bytes"
    assert was is False
