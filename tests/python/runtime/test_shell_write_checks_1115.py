"""Files a shell command wrote get the edit checks."""
from __future__ import annotations

import collections
import subprocess
import time
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _shell_writes as W


def _repo(tmp_path):
    r = tmp_path / "proj"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "__init__.py").write_text("from .mod import f\n__all__ = ['f']\n")
    (r / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("add", "-A"); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    return r


def _st():
    return SimpleNamespace(git_off=False, git_root=None, tree_cache=collections.OrderedDict(), file_hashes={},
                           edits_made=0)


def test_a_shell_write_is_seen_once_and_an_edit_tool_write_is_not(tmp_path):
    r = _repo(tmp_path)
    st = _st()
    ok = {"ok": True, "code": 0, "stdout": ""}
    assert W.shell_edits(st, "run_command", ok, str(r)) == []          # baseline
    time.sleep(1.1)                       # past git's racy-timestamp window
    (r / "pkg" / "mod.py").write_text("def f():\n    return 22\n")    # python -c "open(...,'w')"
    got = W.shell_edits(st, "run_command", ok, str(r))
    assert [p for _, p in got] == [str(r / "pkg" / "mod.py")]
    assert W.shell_edits(st, "run_command", ok, str(r)) == []          # unchanged since
    (r / "pkg" / "mod.py").write_text("def f():\n    return 333\n")
    from aiforge_core.runtime.chat_agent._turn._progress import _cached_digest
    st.file_hashes[str(r / "pkg" / "mod.py")] = _cached_digest(st, str(r / "pkg" / "mod.py"))
    assert W.shell_edits(st, "run_command", ok, str(r)) == []          # an edit tool wrote it
    assert W.shell_edits(st, "file_patch", ok, str(r)) == []           # not a shell tool


def test_off_switch(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    st = _st()
    monkeypatch.setenv("AIFORGE_CHAT_SHELL_WRITE_CHECKS", "0")
    W.shell_edits(st, "run_command", {"ok": True}, str(r))
    (r / "pkg" / "mod.py").write_text("x = 1\n")
    assert W.shell_edits(st, "run_command", {"ok": True}, str(r)) == []
