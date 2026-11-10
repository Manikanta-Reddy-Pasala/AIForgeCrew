"""A whole-file write that drops the turn's own earlier edits is refused:
sqlglot v2a went from 19/25 to 3/25 when the model rewrote parser.py from
memory of the original and lost the trigger parser it had added."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _rewrite_guard as R

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"]}
ORIG = "".join(f"line_{i} = {i}\n" for i in range(30))
ADDED = "".join(f"def parse_trigger_{i}(self):\n    return {i}\n" for i in range(6))


def _repo(tmp_path):
    (tmp_path / "parser.py").write_text(ORIG)
    for c in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True, env=ENV)
    (tmp_path / "parser.py").write_text(ORIG + ADDED)        # the turn's own edit
    return SimpleNamespace(file_hashes={str(tmp_path / "parser.py"): "x"})


def test_a_rewrite_from_the_original_is_refused_and_force_lets_it_through(tmp_path):
    st = _repo(tmp_path)
    args = {"path": "parser.py", "content": ORIG + "line_new = 1\n"}
    res = R.refusal(st, "file_write", args, str(tmp_path))
    assert res and res["error"] == "rewrite_drops_your_edits" and "parse_trigger_0" in res["hint"]
    reworded = ADDED.replace("return", "return  ")                # same defs, other lines
    assert R.refusal(st, "file_write", {"path": "parser.py", "content": ORIG + reworded},
                     str(tmp_path)) is None
    assert R.refusal(st, "file_write", {**args, "drop_edits": True}, str(tmp_path)) is None
    assert R.refusal(st, "file_write", {**args, "force": True}, str(tmp_path))  # not its key


def test_a_rewrite_that_keeps_the_turns_lines_or_a_file_not_edited_passes(tmp_path):
    st = _repo(tmp_path)
    keep = {"path": "parser.py", "content": ORIG + ADDED + "x_more = 1\n"}
    assert R.refusal(st, "file_write", keep, str(tmp_path)) is None
    st.file_hashes = {}
    assert R.refusal(st, "file_write", {"path": "parser.py", "content": ORIG},
                     str(tmp_path)) is None


def test_a_file_new_this_turn_is_a_draft_and_may_be_rewritten(tmp_path):
    st = _repo(tmp_path)
    (tmp_path / "new_mod.py").write_text(ADDED)
    st.file_hashes[str(tmp_path / "new_mod.py")] = "y"
    assert R.refusal(st, "file_write", {"path": "new_mod.py", "content": "x = 1\n"},
                     str(tmp_path)) is None


def _plain_repo(tmp_path, text):
    (tmp_path / "strutils.py").write_text(text)
    for c in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True, env=ENV)
    return SimpleNamespace(file_hashes={})


OLD = "".join(f"def helper_{i}(x):\n    return x + {i}\n\n" for i in range(12))


def test_a_rewrite_that_drops_the_repos_own_functions_is_refused(tmp_path):
    """boltons fr1a: strutils.py written whole from memory to add ellipsize;
    a dozen old functions were gone and 10 old tests failed."""
    st = _plain_repo(tmp_path, OLD)
    new = "".join(f"def helper_{i}(x):\n    return x + {i}\n\n" for i in range(3)) + \
        "def ellipsize(text, max_len=160):\n    return text\n"
    res = R.refusal(st, "file_write", {"path": "strutils.py", "content": new}, str(tmp_path))
    assert res and res["error"] == "rewrite_drops_existing_code"
    assert "helper_3" in res["lost_defs"] and "file_patch" in res["hint"]
    assert R.refusal(st, "file_write", {"path": "strutils.py", "content": new,
                                        "drop_edits": True}, str(tmp_path)) is None


def test_a_rewrite_that_keeps_every_function_passes(tmp_path):
    st = _plain_repo(tmp_path, OLD)
    new = OLD.replace("return x +", "return  x +") + "def ellipsize(t):\n    return t\n"
    assert R.refusal(st, "file_write", {"path": "strutils.py", "content": new},
                     str(tmp_path)) is None


def test_a_function_already_removed_by_an_edit_is_not_counted(tmp_path):
    """Deleted earlier with file_patch (on purpose): a later whole write
    without it loses nothing the file still has."""
    st = _plain_repo(tmp_path, OLD)
    (tmp_path / "strutils.py").write_text(OLD.replace("def helper_5(x):\n    return x + 5\n\n", ""))
    new = OLD.replace("def helper_5(x):\n    return x + 5\n\n", "")
    assert R.refusal(st, "file_write", {"path": "strutils.py", "content": new},
                     str(tmp_path)) is None
