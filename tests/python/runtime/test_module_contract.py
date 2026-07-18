"""Parallel-decompose cohesion fix: every subtask's brief carries a shared
symbol→module map so isolated worktrees can't diverge on where a shared symbol
lives (the `from .queue import X` vs class-in-core.py failure)."""
from __future__ import annotations

from aiforge_core.runtime.parallel_subtasks._planning import (
    _module_contract, _plan_files,
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


def test_plan_files_still_unique_slugs_and_paths():
    # regression: the contract injection must not disturb slug/path handling
    subs = _plan_files([
        {"path": "a/db.py", "purpose": "x", "api": ["class A"]},
        {"path": "b/db.py", "purpose": "y", "api": ["class B"]}])
    slugs = [s["slug"] for s in subs]
    assert len(slugs) == len(set(slugs))      # disambiguated
    assert {s["path"] for s in subs} == {"a/db.py", "b/db.py"}
