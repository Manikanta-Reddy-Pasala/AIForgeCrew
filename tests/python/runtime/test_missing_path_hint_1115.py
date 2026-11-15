"""A read of a missing file says where the work is."""
from __future__ import annotations

import subprocess

from aiforge_core.runtime.chat_agent._tools import _missing as M
from aiforge_core.runtime.chat_agent._tools._code import _t_read_lines


def _repo(tmp_path):
    r = tmp_path / "proj"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "recipes.py").write_text("x = 1\n")
    (r / "pkg" / "more.py").write_text("y = 2\n")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


def test_a_path_outside_the_repo_says_so(tmp_path):
    r = _repo(tmp_path)
    out = _t_read_lines({"path": "/home/someone/.cache/mic/mic.py"}, str(r))
    assert not out["ok"] and "outside the repository" in out["hint"]
    assert "do not search the rest of the machine" in out["hint"]


def test_a_misnamed_file_gets_the_close_repo_names(tmp_path):
    r = _repo(tmp_path)
    out = _t_read_lines({"path": "pkg/recipe.py"}, str(r))
    assert out["similar"] == ["pkg/recipes.py"]


def test_a_same_named_file_elsewhere_in_the_repo_is_named(tmp_path):
    r = _repo(tmp_path)
    out = _t_read_lines({"path": "more.py"}, str(r))
    assert out["similar"] == ["pkg/more.py"]


def test_no_close_name_says_to_search_the_repo(tmp_path):
    r = _repo(tmp_path)
    assert "use find or grep" in M.hint("zzz_nothing.py", str(r))["hint"]
