"""Colour is on for a terminal and off for everything else."""

from __future__ import annotations

import io

from aiforge_cli import colors


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_a_terminal_gets_colour():
    assert colors.detect(_Tty(), {"TERM": "xterm-256color"}).enabled is True


def test_a_pipe_gets_none():
    assert colors.detect(io.StringIO(), {"TERM": "xterm"}).enabled is False


def test_no_color_wins_over_a_terminal():
    assert colors.detect(_Tty(), {"NO_COLOR": "1", "TERM": "xterm"}).enabled is False


def test_a_dumb_terminal_gets_none():
    assert colors.detect(_Tty(), {"TERM": "dumb"}).enabled is False


def test_windows_has_no_term_variable_and_still_gets_colour():
    # TERM is normally unset on Windows; treating that as "no colour" made the
    # whole Windows binary monochrome.
    assert colors.detect(_Tty(), {"OS": "Windows_NT"}).enabled is True


def test_always_forces_colour_through_a_pipe():
    assert colors.detect(io.StringIO(), {"AIFORGE_CLI_COLOR": "always"}).enabled is True


def test_a_disabled_palette_returns_the_text_unchanged():
    pal = colors.Palette(False)
    assert pal("hello", "ok") == "hello"
    assert "\033[32m" in colors.Palette(True)("hello", "ok")
