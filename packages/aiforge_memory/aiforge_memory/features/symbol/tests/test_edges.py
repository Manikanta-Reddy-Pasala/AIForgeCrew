"""L4 — call edge resolution: same-file > import > fuzzy."""
from __future__ import annotations

from pathlib import Path

from aiforge_memory.features.symbol import extract as tsw
from aiforge_memory.features.symbol import extract_calls as edges

FIX = Path(__file__).parent / "fixtures" / "poly_repo"


def test_python_same_file_call_resolves_with_high_confidence() -> None:
    files = tsw.walk_repo(FIX, repo="poly_test")
    call_edges = edges.resolve_calls_with_source(
        files, repo="poly_test", repo_root=FIX,
    )
    # main() calls PaymentService(...) and svc.process(100.0)
    main_calls = [e for e in call_edges
                  if e.caller_fqname == "api/main.py::main"]
    # must find at least the same-file process call
    callees = {e.callee_fqname for e in main_calls}
    same_file_high = [e for e in main_calls if e.confidence >= 0.7]
    assert len(same_file_high) >= 1
    assert any("PaymentService" in c for c in callees) or \
           any("process" in c for c in callees)


def test_python_import_aware_call_resolves() -> None:
    files = tsw.walk_repo(FIX, repo="poly_test")
    call_edges = edges.resolve_calls_with_source(
        files, repo="poly_test", repo_root=FIX,
    )
    # PaymentService.process calls helpers.normalize  (imported)
    process_calls = [e for e in call_edges
                     if e.caller_fqname == "api/main.py::PaymentService::process"]
    callees = {e.callee_fqname for e in process_calls}
    assert "api/helpers.py::normalize" in callees


def test_java_same_class_call_resolves() -> None:
    files = tsw.walk_repo(FIX, repo="poly_test")
    call_edges = edges.resolve_calls_with_source(
        files, repo="poly_test", repo_root=FIX,
    )
    process_calls = [
        e for e in call_edges
        if e.caller_fqname == "svc/Service.java::PaymentService::process"
    ]
    callees = {e.callee_fqname for e in process_calls}
    # process() calls normalize() in same class
    assert any("normalize" in c for c in callees)


def test_typescript_class_method_call_resolves() -> None:
    files = tsw.walk_repo(FIX, repo="poly_test")
    call_edges = edges.resolve_calls_with_source(
        files, repo="poly_test", repo_root=FIX,
    )
    boot_calls = [
        e for e in call_edges
        if e.caller_fqname == "web/main.ts::bootstrap"
    ]
    callees = {e.callee_fqname for e in boot_calls}
    assert any("increment" in c for c in callees)
    # imported normalize from ./helpers
    assert "web/helpers.ts::normalize" in callees


def test_no_self_calls_emitted() -> None:
    files = tsw.walk_repo(FIX, repo="poly_test")
    call_edges = edges.resolve_calls_with_source(
        files, repo="poly_test", repo_root=FIX,
    )
    for e in call_edges:
        assert e.caller_fqname != e.callee_fqname


def test_maven_path_strip_for_java_imports() -> None:
    """`com.foo.Bar` should resolve under src/main/java/ prefix."""
    cands = edges._import_candidates("com.example.PaymentService")
    assert "com/example/PaymentService.java" in cands
    assert "src/main/java/com/example/PaymentService.java" in cands
    assert "src/test/java/com/example/PaymentService.java" in cands
    assert "src/main/kotlin/com/example/PaymentService.kt" in cands
