"""L4 — tree-sitter walk: per-language symbol extraction."""
from __future__ import annotations

from pathlib import Path

from aiforge_memory.features.symbol import extract as tsw

FIX = Path(__file__).parent / "fixtures" / "poly_repo"


def test_walk_emits_one_walkedfile_per_source(tmp_path) -> None:
    files = tsw.walk_repo(FIX, repo="poly_test")
    paths = {f.path for f in files}
    assert "api/main.py" in paths
    assert "api/helpers.py" in paths
    assert "svc/Service.java" in paths
    assert "web/main.ts" in paths
    assert "web/helpers.ts" in paths


def test_python_symbols_extracted() -> None:
    files = tsw.walk_repo(FIX, repo="poly_test")
    main = next(f for f in files if f.path == "api/main.py")
    fqnames = {s.fqname for s in main.symbols}
    # Class
    assert "api/main.py::PaymentService" in fqnames
    # Methods (should carry parent class in fqname)
    assert "api/main.py::PaymentService::process" in fqnames
    assert "api/main.py::PaymentService::refund" in fqnames
    assert "api/main.py::PaymentService::__init__" in fqnames
    # Top-level functions
    assert "api/main.py::health" in fqnames
    assert "api/main.py::main" in fqnames
    # Imports
    assert "os" in main.imports
    assert "pathlib" in main.imports
    # No parse errors
    assert main.parse_error is False


def test_python_kinds_correct() -> None:
    files = tsw.walk_repo(FIX, repo="poly_test")
    main = next(f for f in files if f.path == "api/main.py")
    by_name = {s.fqname.split("::")[-1]: s.kind for s in main.symbols}
    assert by_name["PaymentService"] == "class"
    assert by_name["process"] == "method"
    assert by_name["health"] == "function"


def test_java_symbols_extracted() -> None:
    files = tsw.walk_repo(FIX, repo="poly_test")
    java = next(f for f in files if f.path == "svc/Service.java")
    fqnames = {s.fqname for s in java.symbols}
    assert "svc/Service.java::PaymentService" in fqnames
    assert "svc/Service.java::PaymentService::process" in fqnames
    assert "svc/Service.java::PaymentService::normalize" in fqnames
    # Constructor (java captures it as a method too)
    assert any("PaymentService::PaymentService" in f for f in fqnames)


def test_typescript_symbols_extracted() -> None:
    files = tsw.walk_repo(FIX, repo="poly_test")
    ts = next(f for f in files if f.path == "web/main.ts")
    fqnames = {s.fqname for s in ts.symbols}
    assert "web/main.ts::Counter" in fqnames
    assert "web/main.ts::Counter::increment" in fqnames
    assert "web/main.ts::Counter::decrement" in fqnames
    assert "web/main.ts::bootstrap" in fqnames
    # Imports
    assert "./helpers" in ts.imports


def test_skip_dirs_ignored(tmp_path) -> None:
    # Place a .py inside node_modules — should NOT be picked up
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.py").write_text("def f(): pass")
    (tmp_path / "kept.py").write_text("def g(): pass")
    files = tsw.walk_repo(tmp_path, repo="x")
    paths = {f.path for f in files}
    assert "kept.py" in paths
    assert "node_modules/ignored.py" not in paths


def test_walked_file_hash_stable(tmp_path) -> None:
    p = tmp_path / "x.py"
    p.write_text("def f(): pass\n")
    f1 = tsw.walk_repo(tmp_path, repo="x")
    f2 = tsw.walk_repo(tmp_path, repo="x")
    assert f1[0].hash == f2[0].hash
    assert len(f1[0].hash) == 64
