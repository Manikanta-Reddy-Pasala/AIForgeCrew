# tests/python/runtime/test_post_edit.py
"""After an edit lands: the project's own formatter runs (only one the
project uses), then the file's errors — never style — go to the model on its
next step, once per distinct set."""
import os
import shutil
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import post_edit as PE
from aiforge_core.runtime.chat_agent._turn import _post_edit as T

needs_ruff = pytest.mark.skipif(PE._ruff() is None, reason="ruff not installed")

UGLY = "x=1\ndef f( a ):\n  return a+x\n"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_POST_EDIT_FORMAT", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_POST_EDIT_DIAGNOSTICS", raising=False)


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return str(p)


@needs_ruff
def test_ruff_formats_only_a_project_that_uses_ruff(tmp_path):
    used = tmp_path / "used"
    _write(used / "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    f = _write(used / "pkg" / "m.py", UGLY)
    assert PE.format_file(f, str(used)) == "ruff"
    assert "def f(a):" in open(f).read()
    assert PE.format_file(f, str(used)) is None          # already formatted: no change said

    plain = tmp_path / "plain"
    g = _write(plain / "m.py", UGLY)
    assert PE.format_file(g, str(plain)) is None
    assert open(g).read() == UGLY                         # no reformat in a repo without ruff


@needs_ruff
def test_format_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_POST_EDIT_FORMAT", "0")
    _write(tmp_path / "ruff.toml", "")
    f = _write(tmp_path / "m.py", UGLY)
    assert PE.format_file(f, str(tmp_path)) is None and open(f).read() == UGLY


def test_no_prettier_in_a_project_without_it(tmp_path):
    f = _write(tmp_path / "a.ts", "const  x =1\n")
    assert PE._formatter(f, str(tmp_path)) is None


def test_a_config_above_the_chat_folder_is_not_the_projects(tmp_path):
    _write(tmp_path / "ruff.toml", "")                    # e.g. in the home folder
    work = tmp_path / "work"
    f = _write(tmp_path / "elsewhere" / "m.py", UGLY)     # a file outside the chat folder
    work.mkdir()
    assert list(PE._up_to(f, str(work))) == [os.path.dirname(f)]
    assert not PE._uses_ruff(f, str(work))


@needs_ruff
def test_python_errors_only_never_style(tmp_path):
    bad = _write(tmp_path / "bad.py", "import os\n\ndef f():\n    return undefined_name\n")
    out = PE.diagnostics(bad, str(tmp_path))
    assert len(out) == 1 and "F821" in out[0] and "undefined_name" in out[0]   # unused os: style, not said
    ok = _write(tmp_path / "ok.py", "import os\nx=os.sep\n")
    assert PE.diagnostics(ok, str(tmp_path)) == []


@needs_ruff
def test_diagnostics_off_switch_and_unknown_suffix(tmp_path, monkeypatch):
    bad = _write(tmp_path / "bad.py", "def f():\n    return nope\n")
    assert PE.diagnostics(_write(tmp_path / "a.txt", "nope"), str(tmp_path)) is None
    monkeypatch.setenv("AIFORGE_CHAT_POST_EDIT_DIAGNOSTICS", "0")
    assert PE.diagnostics(bad, str(tmp_path)) is None


@pytest.mark.skipif(not shutil.which("go"), reason="go not installed")
def test_go_vet_errors_for_the_file(tmp_path):
    _write(tmp_path / "go.mod", "module x\n\ngo 1.21\n")
    f = _write(tmp_path / "main.go", "package main\n\nfunc main() {\n\ty := missing\n\t_ = y\n}\n")
    out = PE.diagnostics(f, str(tmp_path))
    assert out and "main.go" in out[0]


@needs_ruff
def test_ruff_keeps_the_projects_exclude_and_nothing_outside_the_folder(tmp_path):
    _write(tmp_path / "pyproject.toml", '[tool.ruff]\nexclude = ["migrations"]\n')
    f = _write(tmp_path / "migrations" / "m.py", UGLY)
    assert PE.format_file(f, str(tmp_path)) is None and open(f).read() == UGLY
    out = tmp_path.parent / f"{tmp_path.name}-other" / "m.py"
    g = _write(out, UGLY)
    _write(out.parent / "ruff.toml", "")
    assert PE.format_file(g, str(tmp_path)) is None and open(g).read() == UGLY


def test_go_lines_are_this_files_only(monkeypatch, tmp_path):
    _write(tmp_path / "go.mod", "module x\n")
    f = _write(tmp_path / "main.go", "package main\n")
    monkeypatch.setattr(PE.shutil, "which", lambda n: "/usr/bin/go")
    monkeypatch.setattr(PE, "_run", lambda cmd, cwd, timeout=30: SimpleNamespace(
        returncode=1, stdout="", stderr="# x\nvet: ./main.go:4:7: undefined: missing\n"
                                        "./other_main.go:2:1: undefined: y\n"))
    assert PE.diagnostics(f, str(tmp_path)) == ["vet: ./main.go:4:7: undefined: missing"]
    monkeypatch.setattr(PE, "_run", lambda cmd, cwd, timeout=30: None)     # timed out
    assert PE.diagnostics(f, str(tmp_path)) is None


def test_edited_files_follow_the_progress_paths(tmp_path):
    files = T.edited_files("multi_edit", {"edits": [{"path": "a.py"}, {"path": "b.py"}]},
                           {"ok": True}, str(tmp_path))
    assert [x[0] for x in files] == ["a.py", "b.py"]
    assert files[0][1] == os.path.realpath(str(tmp_path / "a.py"))
    assert T.edited_files("file_write", {"path": "a.py"}, {"ok": False}, str(tmp_path)) == []
    assert T.edited_files("file_read", {"path": "a.py"}, {"ok": True}, str(tmp_path)) == []


def test_turn_says_the_same_errors_once(monkeypatch, tmp_path):
    errs = [["m.py:2:5: F821 Undefined name `x`"]]
    monkeypatch.setattr(PE, "diagnostics", lambda path, cwd: errs[0])
    st = SimpleNamespace(convo=[])
    files = [("m.py", str(tmp_path / "m.py"))]
    ev = list(T.post_edit_diagnostics(st, files, str(tmp_path)))
    assert ev and "F821" in st.convo[-1]["content"] and len(st.convo) == 1
    assert list(T.post_edit_diagnostics(st, files, str(tmp_path))) == []
    errs[0] = ["m.py:9:5: F821 Undefined name `x`"]       # lines moved: same error
    assert list(T.post_edit_diagnostics(st, files, str(tmp_path))) == []
    errs[0] = None                                        # checker timed out: not "fixed"
    list(T.post_edit_diagnostics(st, files, str(tmp_path)))
    errs[0] = ["m.py:9:5: F821 Undefined name `x`"]
    assert list(T.post_edit_diagnostics(st, files, str(tmp_path))) == [] and len(st.convo) == 1
    errs[0] = []                                          # fixed
    list(T.post_edit_diagnostics(st, files, str(tmp_path)))
    errs[0] = ["m.py:2:5: F821 Undefined name `x`"]       # broken again: said again
    list(T.post_edit_diagnostics(st, files, str(tmp_path)))
    assert len(st.convo) == 2


def test_turn_notes_a_reformat_and_a_broken_checker_is_quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(PE, "format_file", lambda path, cwd: "ruff")
    st = SimpleNamespace(convo=[])
    files = [("m.py", str(tmp_path / "m.py"))]
    ev = list(T.post_edit_format(st, files, str(tmp_path)))
    assert ev[0]["text"] == "Formatted m.py with ruff"
    assert "Read the lines again" in st.convo[-1]["content"]

    def boom(*_a):
        raise RuntimeError("x")
    monkeypatch.setattr(PE, "diagnostics", boom)
    assert list(T.post_edit_diagnostics(st, files, str(tmp_path))) == []
    assert list(T.post_edit_format(st, [], str(tmp_path))) == []


@needs_ruff
def test_a_chat_folder_behind_a_symlink_still_finds_the_config(tmp_path):
    real = tmp_path / "real"
    _write(real / "ruff.toml", "")
    f = _write(real / "pkg" / "m.py", UGLY)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert PE.format_file(os.path.realpath(f), str(link)) == "ruff"


def test_a_tsconfig_error_is_not_clean(monkeypatch, tmp_path):
    _write(tmp_path / "tsconfig.json", "{}")
    _write(tmp_path / "node_modules" / ".bin" / "tsc", "")
    f = _write(tmp_path / "src" / "a.ts", "let x = 1\n")
    PE._SLOW_TSC.clear()
    run = SimpleNamespace(returncode=2, stdout="error TS18003: No inputs were found\n", stderr="")
    monkeypatch.setattr(PE, "_run", lambda cmd, cwd, timeout=30: run)
    assert PE.diagnostics(f, str(tmp_path)) is None
    run.stdout = "src/a.ts(1,5): error TS2322: bad\nsrc/b.ts(2,1): error TS2322: other\n"
    assert PE.diagnostics(f, str(tmp_path)) == ["src/a.ts(1,5): error TS2322: bad"]
