"""The team-run guard on the user's REAL checkout (runtime/team_repo_guard).

The earlier guard listed only known file writers, so formatters, builds and
inline scripts run against the real repo got through.
"""
from __future__ import annotations

import os

import pytest

from aiforge_core.runtime.team_repo_guard import touches
from aiforge_core.runtime.team_run_life import fold


@pytest.fixture()
def dirs(tmp_path):
    repo = tmp_path / "proj"
    wt = tmp_path / "runs" / "wt"
    (repo / "src").mkdir(parents=True)
    wt.mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n")
    return str(repo), str(wt)


def _hits(cmd, dirs):
    repo, wt = dirs
    return touches(cmd, os.path.realpath(wt), fold(repo))


@pytest.mark.parametrize("tmpl", [
    "cd {r} && prettier --write .",
    "cd {r} && ruff --fix .",
    "cd {r} && npm install",
    "cd {r} && python fix.py",
    "(cd {r} && git commit -am x)",
    "pushd {r} && make",
    "make -C {r}",
    "npm --prefix {r} install",
    "python -c \"open('{r}/src/a.py','w').write('')\"",
    "git --git-dir={r}/.git --work-tree={r} checkout .",
    "git -C {r} commit -am x",
    "sed -i s/1/2/ {r}/src/a.py",
    "echo hi > {r}/src/a.py",
    "cp notes.txt {r}/src/",
    "rm -rf {r}/src",
    "bash -c 'cd {r} && make'",
    "find {r} -name '*.pyc' -delete",
])
def test_writes_and_runs_in_the_real_repo_are_caught(dirs, tmpl):
    assert _hits(tmpl.format(r=dirs[0]), dirs), tmpl


def test_a_variable_naming_the_repo_is_expanded(dirs, monkeypatch):
    monkeypatch.setenv("REPO_UNDER_TEST", dirs[0])
    assert _hits("prettier --write $REPO_UNDER_TEST/src", dirs)


@pytest.mark.parametrize("tmpl", [
    "cat {r}/src/a.py",
    "grep -rn x {r}/src",
    "ls {r}",
    "diff {r}/src/a.py src/a.py",
    "cp {r}/.env .",
    "rsync -a {r}/node_modules/ node_modules/",
    "git -C {r} status",
    "git -C {r} log --oneline -5",
    "find {r} -name '*.py'",
    "npm install",
    "mkdir -p ~/.cache/tool",
    "pytest -q",
    "cat > cfg <<EOF\nroot={r}\nEOF",
])
def test_reads_copies_from_and_work_elsewhere_pass(dirs, tmpl):
    assert _hits(tmpl.format(r=dirs[0]), dirs) == [], tmpl


def test_a_sibling_with_the_same_prefix_is_not_the_repo(dirs, tmp_path):
    (tmp_path / "proj-old").mkdir()
    assert _hits(f"rm -rf {tmp_path}/proj-old", dirs) == []


def test_litellm_connection_error_is_a_refused_socket_not_busy():
    from aiforge_core.llm import _probe_states as ps

    class APIConnectionError(Exception):
        status_code = 500
        response = None
    st = ps.exc_state(APIConnectionError("Connection refused"))
    assert st == ps.REFUSED
