from __future__ import annotations

from aiforge_core.runtime.tools.agentskills import (
    BOOTSTRAP_SOURCE, bootstrap_code,
)


def test_bootstrap_returns_source_string():
    src = bootstrap_code()
    assert isinstance(src, str)
    assert src is BOOTSTRAP_SOURCE


def test_bootstrap_exposes_all_helpers():
    src = bootstrap_code()
    for fn in ("open_file", "goto_line", "find_file",
               "search_dir", "search_file", "create_file", "run_cmd"):
        assert f"def {fn}(" in src


def test_bootstrap_is_compilable():
    src = bootstrap_code()
    code = compile(src, "<agentskills>", "exec")
    ns: dict = {}
    exec(code, ns)
    # Every advertised helper must be callable.
    for fn in ("open_file", "goto_line", "find_file",
               "search_dir", "search_file", "create_file", "run_cmd"):
        assert callable(ns[fn]), f"{fn} missing"


def test_helpers_work_when_executed(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    (tmp_path / "foo.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    src = bootstrap_code()
    ns: dict = {}
    exec(compile(src, "<test>", "exec"), ns)

    out = ns["open_file"]("foo.py", line=2, context=1)
    assert "b" in out and ">>" in out

    hits = ns["find_file"]("foo.py")
    assert "foo.py" in hits

    hits = ns["search_file"]("c", "foo.py")
    assert hits == [{"line": 3, "text": "c"}]
