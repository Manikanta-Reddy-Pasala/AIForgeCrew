"""Every approval gate decides "does this call write files?" from ONE list.

There were four private copies and they had drifted. The team pipeline's
review gate did not list multi_edit, rename_symbol or format — all three handed
to the Doer — so with "review edits" armed they changed files with no
Approve/Reject prompt.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from aiforge_core.runtime import chat_resume, text_doer, tool_gate
from aiforge_core.runtime.chat_agent import _registry
from aiforge_core.runtime.tools.mutating import FILE_WRITE_TOOLS, writes_files

#: The Doer's own tools that change files (runtime/doer_tools/_tools.py).
DOER_FILE_WRITERS = ("write", "patch", "edit", "str_replace", "multi_edit",
                     "format", "rename_symbol")


@pytest.mark.parametrize("tool", DOER_FILE_WRITERS)
def test_the_pipeline_review_gate_holds_every_doer_edit(tool):
    """The regression: these reached the workspace without review."""
    assert tool_gate._is_mutating(tool, {}), f"{tool} would skip review-edits"


@pytest.mark.parametrize("tool", DOER_FILE_WRITERS)
def test_the_chat_review_gate_holds_them_too(tool):
    assert _registry._is_mutating(tool, {})


def test_both_gates_use_the_same_list():
    assert tool_gate._MUTATING is FILE_WRITE_TOOLS
    assert _registry._MUTATING is FILE_WRITE_TOOLS
    assert chat_resume._EDIT_TOOLS is FILE_WRITE_TOOLS


def test_editor_view_is_not_a_write():
    assert not writes_files("editor", {"command": "view"})
    assert writes_files("editor", {"command": "str_replace"})
    assert not tool_gate._is_mutating("editor", {"command": "view"})
    assert not _registry._is_mutating("editor", {"sub_command": "cat"})


def test_shells_are_not_classified_by_name():
    """A shell is judged by what the command does (the risk classifier)."""
    for shell in ("bash", "run_command", "shell", "run"):
        assert not writes_files(shell, {"cmd": "rm -rf x"})


def test_the_chat_loop_counts_every_real_edit():
    """_edits_made drives the verify gate and the false-claim guard."""
    from aiforge_core.runtime.chat_agent._context import _verify
    for tool in ("write", "rename_symbol", "format", "multi_edit", "file_create"):
        assert tool in _verify._EDIT_TOOL_NAMES, tool


def test_the_session_ledger_records_every_file_edit():
    """A resumed session reads the ledger to avoid redoing work."""
    from aiforge_core.runtime import session_ledger as sl
    for tool in ("str_replace", "write", "rename_symbol", "edit", "file_write"):
        entry = sl._summarize(tool, {"path": "a.py"}, {"ok": True})
        assert entry is not None, f"{tool} is invisible to the ledger"
    assert sl._summarize("str_replace", {"path": "a.py"}, {"ok": True})["label"] \
        == "patched `a.py`"
    assert sl._summarize("write", {"path": "a.py"}, {"ok": True})["label"] \
        == "wrote `a.py`"


def test_the_no_edit_guard_counts_created_files_but_not_a_format_pass():
    assert "file_create" in text_doer._EDIT_TOOLS      # was missing
    assert "format" not in text_doer._EDIT_TOOLS       # deliberate


def test_no_module_grows_a_private_copy_again():
    """Guard the fix itself: a module-level EDIT/MUTATING/WRITE-TOOLS list of
    literal names anywhere else is exactly how the drift started (five copies).
    Lists with another job — an export list, a perf-page classifier that mixes
    read tools in — are named differently and are not flagged."""
    import re as _re
    root = pathlib.Path(__file__).resolve().parents[3] / "aiforge_core"
    markers = {"file_write", "file_patch", "str_replace", "editor"}
    classifier_name = _re.compile(r"EDIT|MUTAT|WRITE", _re.I)
    offenders = []
    for f in root.rglob("*.py"):
        if f.name == "mutating.py":
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and classifier_name.search(t.id)
                       for t in node.targets):
                continue
            val = node.value
            if isinstance(val, ast.Call) and val.args:
                val = val.args[0]
            if isinstance(val, (ast.Set, ast.Tuple, ast.List)):
                names = {e.value for e in val.elts
                         if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                if len(names & markers) >= 2:
                    offenders.append(f"{f.relative_to(root)}:{node.lineno}")
    assert not offenders, f"private edit-tool lists: {offenders}"
