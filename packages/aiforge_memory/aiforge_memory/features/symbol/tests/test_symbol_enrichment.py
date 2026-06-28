"""L13 — Symbol enrichment: visibility / modifiers / return_type / params."""
from __future__ import annotations

import json
from pathlib import Path

from aiforge_memory.features.symbol import extract as tsw

FIX = (Path(__file__).parent / "fixtures" / "poly_repo")


def _walk():
    return tsw.walk_repo(FIX, repo="t")


def test_python_protected_visibility_for_dunder_init():
    files = _walk()
    py = next(f for f in files if f.path == "api/main.py")
    init = next(s for s in py.symbols if s.fqname.endswith("::__init__"))
    assert init.visibility == "protected"


def test_python_public_visibility_for_normal_methods():
    files = _walk()
    py = next(f for f in files if f.path == "api/main.py")
    process = next(s for s in py.symbols if s.fqname.endswith("::process"))
    assert process.visibility == "public"


def test_python_return_type_annotation():
    files = _walk()
    py = next(f for f in files if f.path == "api/main.py")
    health = next(s for s in py.symbols if s.fqname.endswith("::health"))
    assert health.return_type == "dict"


def test_python_params_with_type_hints():
    files = _walk()
    py = next(f for f in files if f.path == "api/main.py")
    process = next(s for s in py.symbols if s.fqname.endswith("::process"))
    params = json.loads(process.params_json)
    names = [p["name"] for p in params]
    assert "self" in names and "amount" in names
    amount = next(p for p in params if p["name"] == "amount")
    assert amount["type"] == "float"


def test_java_visibility_public_and_private():
    files = _walk()
    java = next(f for f in files if f.path == "svc/Service.java")
    by_name = {s.fqname: s for s in java.symbols}
    process = by_name["svc/Service.java::PaymentService::process"]
    normalize = by_name["svc/Service.java::PaymentService::normalize"]
    assert process.visibility == "public"
    assert normalize.visibility == "private"


def test_java_return_type_and_params():
    files = _walk()
    java = next(f for f in files if f.path == "svc/Service.java")
    process = next(
        s for s in java.symbols
        if s.fqname.endswith("::PaymentService::process")
    )
    assert process.return_type == "boolean"
    params = json.loads(process.params_json)
    assert params == [{"name": "amount", "type": "double"}]


def test_typescript_return_type_void():
    files = _walk()
    ts = next(f for f in files if f.path == "web/main.ts")
    boot = next(s for s in ts.symbols if s.fqname.endswith("::bootstrap"))
    assert boot.return_type == "void"


def test_typescript_visibility_default_public():
    files = _walk()
    ts = next(f for f in files if f.path == "web/main.ts")
    inc = next(s for s in ts.symbols if s.fqname.endswith("::increment"))
    assert inc.visibility == "public"
