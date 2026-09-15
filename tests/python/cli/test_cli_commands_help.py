"""One table, every surface — and a test that says so."""

from __future__ import annotations

from aiforge_cli import commands as tbl
from aiforge_cli import completion
from aiforge_cli import help as helptext
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


def test_subcommand_choices_are_attached_to_their_own_command():
    # Asserting "the word appears somewhere in the script" stayed green even
    # with every per-command branch deleted. Check the branch itself.
    bash = completion.script("bash")
    branch = next(line for line in bash.splitlines() if line.strip().startswith("box)"))
    for action in tbl.BOX_ACTIONS:
        assert action in branch, f"box {action} missing from its bash branch"
    zsh = completion.script("zsh")
    zsh_branch = next(line for line in zsh.splitlines() if "box)" in line)
    for action in tbl.BOX_ACTIONS:
        assert action in zsh_branch
    fish = completion.script("fish")
    assert "__fish_seen_subcommand_from box" in fish
    assert "'box'" in completion.script("powershell")


def test_a_command_with_no_choices_gets_no_stale_branch():
    bash = completion.script("bash")
    assert "sessions)" not in bash          # nothing to complete after it


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
