"""Signatures a request states for a typed language are checked in the code."""
from aiforge_core.runtime import typed_contract as T

REQ = ("The unit test will call: `new com.x.Revoker(String url, String token)` and "
       "`void passwordChanged(String phone)`, and `static double com.x.Mis.incomeAmount("
       "List<com.x.Acct> roots)`. Run `mvn -o -q test -Dtest=...`; e.g. `passwordChanged(\"98\")`.")


def _write(tmp_path, name, body):
    p = tmp_path / "src" / "main" / "java" / f"{name}.java"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_claims_skip_call_examples_and_commands():
    kinds = [(c[0], c[1], c[2], c[3], c[4]) for c in T.claims(REQ)]
    assert ("ctor", "Revoker", "Revoker", ["String", "String"], None) in kinds
    assert ("method", None, "passwordChanged", ["String"], "void") in kinds
    assert ("method", "Mis", "incomeAmount", ["List"], "double") in kinds
    assert len(kinds) == 3


def test_matching_code_with_annotations_has_no_gaps(tmp_path):
    _write(tmp_path, "Revoker", 'public class Revoker {\n public Revoker(@Value("${a:}") String url,\n'
           ' @Value("${b:}") final String token) { }\n public void passwordChanged(String phone) { }\n}')
    _write(tmp_path, "Mis", "class Mis { static double incomeAmount(List<Acct> roots) { return 0; } }")
    assert T.missing(REQ, str(tmp_path)) == []


def test_wrong_types_and_return_are_named(tmp_path):
    _write(tmp_path, "Revoker", "public class Revoker {\n public Revoker(WebClient web, String token) { }\n"
           " public Mono<Void> passwordChanged(String phone) { return null; }\n}")
    gaps = T.missing(REQ, str(tmp_path))
    assert any("Revoker(WebClient, String)" in g for g in gaps)
    assert any("returning Mono" in g for g in gaps)
    assert any("incomeAmount" in g and "not declared" in g for g in gaps)


def test_no_typed_signature_no_claims():
    assert T.claims("The GST summary shows too much tax. Fix it with a unit test.") == []


def test_a_qualified_class_in_the_wrong_package_is_named(tmp_path):
    req = ("The unit test will call: `static Optional<Found> com.x.health.Detector.find(ThreadInfo[] t)`.")
    p = tmp_path / "src" / "main" / "java" / "com" / "x" / "lock" / "Detector.java"
    p.parent.mkdir(parents=True)
    p.write_text("package com.x.lock;\nclass Detector { static Optional<Found> find(ThreadInfo[] t) { return null; } }")
    gaps = T.missing(req, str(tmp_path))
    assert any("package com.x.lock, not com.x.health" in g for g in gaps)
    p.write_text(p.read_text().replace("package com.x.lock;", "package com.x.health;"))
    assert not any("package" in g for g in T.missing(req, str(tmp_path)))
