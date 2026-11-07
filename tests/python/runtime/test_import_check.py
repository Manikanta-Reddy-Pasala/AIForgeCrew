"""After an edit, the edited package must still import (attrs n3, 0/18:
a function called while _make.py was still loading; the file linted clean)."""
from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _post_edit as P


def _repo(tmp_path):
    pkg = tmp_path / "src" / "pk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from pk.core import f\n")
    (pkg / "core.py").write_text("def f():\n    return 1\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for c in (["init", "-q"], ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True, env=env)
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    st = SimpleNamespace(cwd=str(tmp_path), head0=head,
                         last_test_cmd=(1, {"cmd": f"{sys.executable} -m pytest -q"}),
                         last_full_test=None)
    return st, pkg / "core.py"


def test_a_name_used_before_its_definition_at_import_is_caught(tmp_path, monkeypatch):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "command_of", lambda x: x.get("cmd"))
    st, core = _repo(tmp_path)
    core.write_text("X = later()\n\ndef f():\n    return 1\n\ndef later():\n    return 2\n")
    err = P.import_error(st, str(core))
    assert err and "NameError" in err and "later" in err and "pk.core" in err


def test_a_working_package_or_no_project_python_says_nothing(tmp_path, monkeypatch):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "command_of", lambda x: x.get("cmd"))
    st, core = _repo(tmp_path)
    assert P.import_error(st, str(core)) is None
    core.write_text("X = later()\n")
    st.last_test_cmd = None
    assert P.import_error(st, str(core)) is None                  # no interpreter known


def test_an_import_broken_at_the_base_too_is_not_this_turns(tmp_path, monkeypatch):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "command_of", lambda x: x.get("cmd"))
    st, core = _repo(tmp_path)
    monkeypatch.setattr(P, "_try_import", lambda py, root, mod: "NameError: x")
    assert P.import_error(st, str(core)) is None


def test_a_slow_import_is_not_checked_again(tmp_path, monkeypatch):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "command_of", lambda x: x.get("cmd"))
    st, core = _repo(tmp_path)
    calls = []
    monkeypatch.setattr(P, "_try_import", lambda py, root, mod: calls.append(1) or "timeout")
    assert P.import_error(st, str(core)) is None and P.import_error(st, str(core)) is None
    assert len(calls) == 1


def test_package_roots_for_modules_and_inits(tmp_path):
    tmp_path = tmp_path.resolve()
    sub = tmp_path / "src" / "pk" / "sub"
    sub.mkdir(parents=True)
    for d in (tmp_path / "src" / "pk", sub):
        (d / "__init__.py").write_text("")
    (sub / "m.py").write_text("")
    src = str(tmp_path / "src")
    assert P._package_root(str(sub / "m.py")) == (src, "pk.sub.m")
    assert P._package_root(str(sub / "__init__.py")) == (src, "pk.sub")
    assert P._package_root(str(tmp_path / "src" / "pk" / "__init__.py")) == (src, "pk")


def test_missing_modules_count_only_when_they_are_the_projects(tmp_path):
    pkg = tmp_path / "pk"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import pk.inspect\n")
    assert "pk.inspect" in P._try_import(sys.executable, str(tmp_path), "pk")
    (pkg / "__init__.py").write_text("import torch_not_installed_xyz\n")
    assert P._try_import(sys.executable, str(tmp_path), "pk") is None     # optional dep
    assert P._try_import(sys.executable, str(tmp_path / "nowhere"), "pk") is None  # path guess
