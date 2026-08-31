"""Deterministic compaction of noisy tool output.

The contract: pull the failure lines out of a 200-line scrollback, keep them in
order, drop duplicates, and stay hard-capped so the digest can never re-bloat
the context it exists to shrink. Pure regex — a green command with nothing
salient must produce NOTHING, or every successful step pays for a digest.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.output_compactor import digest, key_lines


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for k in ("AIFORGE_COMPACT_OUTPUT", "AIFORGE_COMPACT_MAX_LINES",
              "AIFORGE_COMPACT_MAX_CHARS"):
        monkeypatch.delenv(k, raising=False)


# ─── key_lines ─────────────────────────────────────────────────────────


def test_pulls_the_signal_out_of_scrollback():
    text = "\n".join(["collecting ..."] * 50
                     + ["E   NullPointerException at Foo.java:42"]
                     + ["ok"] * 50)
    assert key_lines(text) == ["E   NullPointerException at Foo.java:42"]


def test_order_is_preserved_and_duplicates_collapse():
    text = "\n".join([
        "Traceback (most recent call last):",
        "  at foo.bar(Foo.java:1)",
        "Traceback (most recent call last):",   # repeat — dropped
        "ValueError: bad input",
    ])
    assert key_lines(text) == [
        "Traceback (most recent call last):",
        "  at foo.bar(Foo.java:1)",
        "ValueError: bad input",
    ]


def test_line_cap_is_honoured():
    text = "\n".join(f"error {i}" for i in range(100))
    assert len(key_lines(text, max_lines=3)) == 3


def test_line_cap_comes_from_the_env_when_not_passed(monkeypatch):
    monkeypatch.setenv("AIFORGE_COMPACT_MAX_LINES", "2")
    text = "\n".join(f"error {i}" for i in range(10))
    assert len(key_lines(text)) == 2


def test_a_junk_env_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("AIFORGE_COMPACT_MAX_LINES", "not-a-number")
    text = "\n".join(f"error {i}" for i in range(50))
    assert len(key_lines(text)) == 24


@pytest.mark.parametrize("text", ["", None])
def test_empty_text(text):
    assert key_lines(text) == []


def test_clean_output_has_no_key_lines():
    assert key_lines("all 42 tests passed\nbuild complete\n") == []


@pytest.mark.parametrize("line", [
    "FAILED tests/test_x.py::test_y",
    "fatal: not a git repository",
    "panic: runtime error: index out of range",
    "assert 1 == 2",
    "No such file or directory",
    "cannot find symbol",
    "SyntaxError: invalid syntax",
    "::error file=app.ts,line=3::Type error",
    "exit code 1",
    "Segmentation fault",
])
def test_failure_shapes_across_languages(line):
    assert key_lines(f"noise\n{line}\nmore noise") == [line]


# ─── digest ────────────────────────────────────────────────────────────


def test_stderr_leads_and_stdout_does_not_repeat_it():
    out = digest(stdout="error: boom\nplain line", stderr="error: boom", returncode=1)
    assert out.startswith("exit=1 · key lines:")
    assert out.count("error: boom") == 1


def test_green_run_produces_nothing():
    assert digest(stdout="all good\n", stderr="", returncode=0) == ""


def test_no_returncode_and_no_signal_produces_nothing():
    assert digest(stdout="all good\n") == ""


def test_nonzero_exit_with_a_silent_command_still_reports():
    assert digest(stdout="", stderr="", returncode=2) == "exit=2 · command failed"


def test_returncode_is_omitted_when_unknown():
    out = digest(stdout="ValueError: bad")
    assert out.startswith("key lines:")
    assert "exit=" not in out


def test_digest_is_hard_capped(monkeypatch):
    monkeypatch.setenv("AIFORGE_COMPACT_MAX_CHARS", "40")
    monkeypatch.setenv("AIFORGE_COMPACT_MAX_LINES", "100")
    out = digest(stderr="\n".join(f"error number {i}" for i in range(100)), returncode=1)
    assert len(out) == 40


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("AIFORGE_COMPACT_OUTPUT", "0")
    assert digest(stdout="error: boom", stderr="error: boom", returncode=1) == ""


@pytest.mark.parametrize("flag", ["0", "false", "no"])
def test_every_off_spelling(monkeypatch, flag):
    monkeypatch.setenv("AIFORGE_COMPACT_OUTPUT", flag)
    assert digest(stderr="error", returncode=1) == ""


def test_on_by_default_and_for_any_other_value(monkeypatch):
    monkeypatch.setenv("AIFORGE_COMPACT_OUTPUT", "1")
    assert digest(stderr="error: boom", returncode=1) != ""
