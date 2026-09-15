"""The status line, and the promise that it never eats the answer."""

from __future__ import annotations

import io

from aiforge_cli.colors import Palette
from aiforge_cli.tail import Tail, _truncate, _visible_len


def test_nothing_is_redrawn_when_the_output_is_not_a_terminal():
    out = io.StringIO()
    tail = Tail(out, enabled=False)
    tail.set("working")
    tail.spin()
    tail.write(["a line"])
    tail.stream("text")
    text = out.getvalue()
    assert text == "a line\ntext"
    assert "\r" not in text
    assert "\033" not in text


def test_a_streamed_answer_survives_the_spinner():
    out = io.StringIO()
    tail = Tail(out, enabled=True)
    tail.set("thinking")
    tail.stream("the answer")
    tail.spin()                      # would have erased the line it sits on
    tail.spin()
    assert out.getvalue().count("the answer") == 1
    assert out.getvalue().endswith("the answer")


def test_committing_after_a_stream_closes_the_line_instead_of_erasing_it():
    out = io.StringIO()
    tail = Tail(out, enabled=True)
    tail.stream("half an answer")
    tail.write(["done"])
    text = out.getvalue()
    assert "half an answer\n" in text
    assert text.index("half an answer") < text.index("done")


def test_the_status_line_is_erased_before_permanent_output():
    out = io.StringIO()
    tail = Tail(out, enabled=True)
    tail.set("working")
    tail.write(["a line"])
    text = out.getvalue()
    assert "\033[2K" in text
    assert "a line\n" in text


def test_truncation_counts_columns_not_escape_bytes():
    pal = Palette(True)
    coloured = pal("hello", "ok") + " world"
    assert _visible_len(coloured) == len("hello world")
    cut = _truncate(coloured, 8)
    assert _visible_len(cut) == 8
    assert cut.endswith("\033[0m")     # never cut mid-escape, never leak colour


def test_a_streamed_fragment_is_separated_from_the_next_line_when_disabled():
    # A redirect has no status line to erase, but the next permanent line still
    # has to start on its own row instead of being glued to the fragment.
    out = io.StringIO()
    tail = Tail(out, enabled=False)
    tail.stream("I'll read the file")
    tail.write(["✓ read_file"])
    assert out.getvalue() == "I'll read the file\n✓ read_file\n"


def test_two_streams_in_a_row_stay_on_one_line():
    out = io.StringIO()
    tail = Tail(out, enabled=True)
    tail.stream("half ")
    tail.stream("an answer")
    assert out.getvalue().endswith("half an answer")
    assert out.getvalue().count("\n") == 0
