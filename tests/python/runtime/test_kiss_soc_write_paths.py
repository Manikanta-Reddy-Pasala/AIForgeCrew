"""Every write surface returns the same KISS / SoC oversize nudge.

Simple chat (file_write / file_patch / multi_edit / editor) and the pipeline
Doer (file_write / file_patch) used to drift: only the structured editor
warned. One helper — syntax_guard.attach_oversize — stamps the warning so
the rule cannot silently leave one surface again.
"""
from __future__ import annotations

import pathlib

import pytest

from aiforge_core.runtime.syntax_guard import attach_oversize, oversize_warning


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    monkeypatch.setenv("AIFORGE_MAX_FILE_LINES", "500")


def test_attach_oversize_stamps_a_successful_write():
    out = attach_oversize({"ok": True, "path": "a.py"}, "a.py", "x=1\n" * 600)
    assert "warning" in out
    assert "500" in out["warning"]


def test_attach_oversize_leaves_a_failed_write_alone():
    out = attach_oversize({"ok": False, "error": "x"}, "a.py", "x=1\n" * 600)
    assert "warning" not in out


def test_attach_oversize_skips_a_small_file():
    out = attach_oversize({"ok": True}, "a.py", "x=1\n")
    assert "warning" not in out


def test_every_production_write_path_calls_the_shared_helper():
    """Guard: a new write surface that forgets the helper fails CI."""
    root = pathlib.Path(__file__).resolve().parents[3] / "aiforge_core" / "runtime"
    paths = {
        "chat_agent/_shell.py": "attach_oversize(",
        "chat_agent/_tools/_skills.py": "attach_oversize(",
        "doer_tools/_fs.py": "attach_oversize(",
        "tools/editor.py": "attach_oversize(",
    }
    for rel, marker in paths.items():
        src = (root / rel).read_text()
        assert marker in src, f"{rel} no longer stamps the KISS/SoC size nudge"


def test_simple_chat_and_doer_prompts_both_teach_the_rule():
    """Text protocol, native chat, and the pipeline Doer all name the rule —
    so neither surface can silently drop the design principle again."""
    from aiforge_core.runtime.chat_agent import _native_prompt, _prompt_text
    from aiforge_core.runtime.prompts import doer
    assert "CLEAN CODE" in _prompt_text._SYSTEM
    assert "500" in _prompt_text._SYSTEM
    assert "500" in _native_prompt.NATIVE_RULES
    doer_src = pathlib.Path(doer.__file__).read_text()
    assert "CLEAN CODE" in doer_src
    assert "500" in doer_src


def test_oversize_warning_text_names_kiss_and_soc():
    w = oversize_warning("x.py", "a=1\n" * 600)
    assert "KISS" in w
    assert "separation of concerns" in w
