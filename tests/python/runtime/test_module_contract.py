"""Parallel-decompose cohesion fix: every subtask's brief carries a shared
symbol→module map so isolated worktrees can't diverge on where a shared symbol
lives (the `from .queue import X` vs class-in-core.py failure)."""
from __future__ import annotations

from aiforge_core.runtime.parallel_subtasks._planning import (
    _coalesce_code_modules, _module_contract, _plan_files, _validate_plan,
)


def _files():
    return [
        {"path": "taskqueue/core.py", "purpose": "queue",
         "api": ["class TaskQueue", "def enqueue(task: dict) -> None"]},
        {"path": "taskqueue/worker.py", "purpose": "worker",
         "api": ["class Worker"]},
        {"path": "taskqueue/__init__.py", "purpose": "package exports", "api": []},
        {"path": "tests/test_core.py", "purpose": "tests", "api": []},
    ]


def test_contract_maps_symbols_to_defining_module():
    c = _module_contract(_files())
    assert "`taskqueue/core.py` defines: class TaskQueue" in c
    assert "class Worker" in c
    # tests + api-less files never appear (nothing imports from them)
    assert "tests/test_core.py" not in c
    assert "__init__.py" not in c
    # names the contract so a model knows not to invent module names
    assert "NEVER invent a module name" in c


def test_contract_empty_when_nothing_cross_module():
    # a single api-bearing module → no cross-module coordination needed
    assert _module_contract([
        {"path": "solo.py", "purpose": "x", "api": ["def f()"]},
        {"path": "tests/test_solo.py", "purpose": "t", "api": []}]) == ""


def test_every_subtask_goal_carries_the_map():
    subs = _plan_files(_files())
    # the __init__ subtask (which must IMPORT TaskQueue) now sees WHERE it lives
    init = next(s for s in subs if s["path"] == "taskqueue/__init__.py")
    assert "taskqueue/core.py` defines: class TaskQueue" in init["goal"]
    # and so does every other buildable subtask
    for s in subs:
        assert "PROJECT MODULE MAP" in s["goal"]


def test_over_fragmentation_gate_reasks_to_consolidate(monkeypatch):
    monkeypatch.setenv("AIFORGE_ARCHITECT_MAX_MODULES", "6")
    # 7 non-test code modules → over-fragmented → an issue naming the cap
    over = ([{"path": f"pkg/m{i}.py", "purpose": "x", "api": []} for i in range(7)]
            + [{"path": "tests/test_a.py", "purpose": "t", "api": []},
               {"path": "pyproject.toml", "purpose": "manifest", "api": []}])
    _clean, issues = _validate_plan(over)
    assert any("over-fragmented" in i and "at most 6" in i for i in issues)


def test_cap_ignores_tests_and_manifest(monkeypatch):
    monkeypatch.setenv("AIFORGE_ARCHITECT_MAX_MODULES", "3")
    # 3 code modules + MANY tests + manifest → tests/manifest don't count → no gate
    plan = ([{"path": f"pkg/m{i}.py", "purpose": "x", "api": []} for i in range(3)]
            + [{"path": f"tests/test_m{i}.py", "purpose": "t", "api": []} for i in range(3)]
            + [{"path": "pyproject.toml", "purpose": "m", "api": []}])
    _clean, issues = _validate_plan(plan)
    assert not any("over-fragmented" in i for i in issues)


def test_hard_coalesce_enforces_python_cap_preserving_symbols(monkeypatch):
    monkeypatch.setenv("AIFORGE_ARCHITECT_MAX_MODULES", "4")
    monkeypatch.delenv("AIFORGE_ARCHITECT_MAX_MODULES_COMPILED", raising=False)
    files = ([{"path": f"pkg/m{i}.py", "purpose": f"p{i}",
               "api": [f"def f{i}()"]} for i in range(6)]
             + [{"path": "tests/test_a.py", "purpose": "t", "api": []},
                {"path": "pyproject.toml", "purpose": "m", "api": []}])
    out, removed = _coalesce_code_modules(files)
    code = [f for f in out if f["path"].endswith(".py")
            and "test" not in f["path"]]
    assert len(code) == 4 and removed == 2          # 6 → 4
    # every original symbol survives (union of apis across merged modules)
    all_api = [a for f in code for a in f["api"]]
    for i in range(6):
        assert f"def f{i}()" in all_api
    # tests + manifest untouched
    assert any(f["path"] == "tests/test_a.py" for f in out)
    assert any(f["path"] == "pyproject.toml" for f in out)


def test_hard_coalesce_tighter_for_compiled(monkeypatch):
    monkeypatch.setenv("AIFORGE_ARCHITECT_MAX_MODULES_COMPILED", "3")
    files = [{"path": f"src/main/java/pkg/M{i}.java", "purpose": "x",
              "api": [f"class M{i}"]} for i in range(6)]
    out, removed = _coalesce_code_modules(files)
    java = [f for f in out if f["path"].endswith(".java")]
    assert len(java) == 3 and removed == 3          # compiled cap 3


def test_coalesce_noop_within_cap():
    files = [{"path": "a.py", "purpose": "x", "api": []},
             {"path": "b.py", "purpose": "y", "api": []}]
    out, removed = _coalesce_code_modules(files)
    assert removed == 0 and out == files


def test_compiled_plan_gets_tighter_cap(monkeypatch):
    monkeypatch.delenv("AIFORGE_ARCHITECT_MAX_MODULES", raising=False)
    monkeypatch.delenv("AIFORGE_ARCHITECT_MAX_MODULES_COMPILED", raising=False)
    # 4 Java modules → over the compiled cap of 3 → consolidate issue
    java = ([{"path": f"src/main/java/pkg/M{i}.java", "purpose": "x", "api": []}
             for i in range(4)]
            + [{"path": "src/test/java/pkg/MTest.java", "purpose": "t", "api": []},
               {"path": "pom.xml", "purpose": "manifest", "api": []}])
    _c, issues = _validate_plan(java)
    assert any("over-fragmented" in i and "at most 3" in i for i in issues)
    # the SAME count in Python is fine (default cap 4)
    py = ([{"path": f"pkg/m{i}.py", "purpose": "x", "api": []} for i in range(4)]
          + [{"path": "tests/test_m.py", "purpose": "t", "api": []}])
    _c2, issues2 = _validate_plan(py)
    assert not any("over-fragmented" in i for i in issues2)


def test_hyphenated_python_module_is_sanitized(monkeypatch):
    monkeypatch.setenv("AIFORGE_ARCHITECT_MAX_MODULES", "9")
    clean, issues = _validate_plan([
        {"path": "pkg/task-queue.py", "purpose": "q", "api": []},
        {"path": "tests/test_task-queue.py", "purpose": "t", "api": []}])
    paths = [f["path"] for f in clean]
    assert "pkg/task_queue.py" in paths           # hyphen → underscore
    assert "pkg/task-queue.py" not in paths
    assert any("aren't importable" in i for i in issues)


def test_plan_files_still_unique_slugs_and_paths():
    # regression: the contract injection must not disturb slug/path handling
    subs = _plan_files([
        {"path": "a/db.py", "purpose": "x", "api": ["class A"]},
        {"path": "b/db.py", "purpose": "y", "api": ["class B"]}])
    slugs = [s["slug"] for s in subs]
    assert len(slugs) == len(set(slugs))      # disambiguated
    assert {s["path"] for s in subs} == {"a/db.py", "b/db.py"}
