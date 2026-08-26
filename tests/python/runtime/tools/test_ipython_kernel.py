from __future__ import annotations

import shutil

import pytest

from aiforge_core.runtime.tools import ipython_kernel as ipy


@pytest.fixture(autouse=True)
def _reset():
    for rid in list(ipy._clients.keys()):
        ipy.destroy_kernel(rid)
    yield
    for rid in list(ipy._clients.keys()):
        ipy.destroy_kernel(rid)


def test_empty_code_rejected():
    out = ipy.execute_ipython_cell("")
    assert out["ok"] is False
    assert out["error"] == "empty_code"


def test_kernel_missing_soft_error(monkeypatch):
    monkeypatch.setattr(ipy, "_jupyter_available", lambda: False)
    out = ipy.execute_ipython_cell("x = 1")
    assert out["ok"] is False
    assert out["error"] == "kernel_missing"


_HAS_JUPYTER = ipy._jupyter_available() and shutil.which("python3")

pytestmark_jupyter = pytest.mark.live_jupyter


@pytestmark_jupyter
def test_state_persists_across_cells():
    run_id = "test-state-persist"
    try:
        a = ipy.execute_ipython_cell("x = 42", _run_id=run_id)
        assert a["ok"], a
        b = ipy.execute_ipython_cell("print(x)", _run_id=run_id)
        assert b["ok"], b
        assert b["stdout"].strip() == "42"
    finally:
        ipy.destroy_kernel(run_id)


@pytestmark_jupyter
def test_syntax_error_surfaces():
    run_id = "test-syntax-err"
    try:
        out = ipy.execute_ipython_cell("def bad(:\n  pass\n", _run_id=run_id)
        assert out["ok"] is False
        assert "SyntaxError" in out["stderr"]
    finally:
        ipy.destroy_kernel(run_id)


@pytestmark_jupyter
def test_expression_returns_result():
    run_id = "test-expr-result"
    try:
        out = ipy.execute_ipython_cell("2 + 3", _run_id=run_id)
        assert out["ok"], out
        assert out["result"] == "5"
    finally:
        ipy.destroy_kernel(run_id)
