"""One table, every surface — and a test that says so."""

from __future__ import annotations

from aiforge_cli import commands as tbl
from aiforge_cli import completion, help as helptext
from aiforge_cli.colors import Palette

PLAIN = Palette(False)


def test_every_top_command_appears_in_the_help():
    text = helptext.top_help(PLAIN, version="0.1.0")
    for cmd in tbl.TOP:
        assert cmd.name in text, f"{cmd.name} missing from `aiforge help`"
        assert cmd.help in text


def test_every_slash_command_appears_in_the_chat_help():
    text = helptext.slash_help(PLAIN)
    for cmd in tbl.SLASH:
        assert cmd.name in text
    for key, _ in tbl.KEYS:
        assert key in text


def test_every_command_is_completable_in_every_shell():
    scripts = {shell: completion.script(shell) for shell in completion.SHELLS}
    for shell, text in scripts.items():
        for cmd in tbl.TOP:
            assert cmd.name in text, f"{cmd.name} is not completable in {shell}"


def test_subcommand_choices_reach_the_shell_scripts():
    for shell in ("bash", "zsh", "fish", "powershell"):
        text = completion.script(shell)
        for action in tbl.BOX_ACTIONS:
            assert action in text, f"box {action} missing from {shell} completion"


def test_an_unknown_shell_is_refused_by_name():
    try:
        completion.script("csh")
    except SystemExit as exc:
        assert "csh" in str(exc)
    else:                                      # pragma: no cover
        raise AssertionError("an unknown shell should not silently succeed")


def test_help_for_one_command_shows_its_examples():
    text = helptext.command_help(PLAIN, "mount")
    assert "aiforge mount add" in text
    assert "approve" in text


def test_help_for_a_slash_command_works_without_the_slash():
    assert "mode" in helptext.command_help(PLAIN, "/mode")
    assert "simple" in helptext.command_help(PLAIN, "mode")


def test_an_unknown_command_lists_the_known_ones():
    text = helptext.command_help(PLAIN, "frobnicate")
    assert "no such command" in text
    assert "box" in text
