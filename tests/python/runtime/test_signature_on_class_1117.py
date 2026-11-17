"""A signature followed by "on `pkg.Class`" belongs to that class."""
from aiforge_core.runtime import typed_contract as tc

REQ = ("The unit test will call: package-private `Mono<Void> saveKeeping(com.acme.model.Settings "
       "incoming, String collectionName)` on `com.acme.sync.SyncServiceImpl`, with its field mocked.")


def test_owner_comes_from_the_class_named_after_the_span():
    cl = tc.claims(REQ)
    assert cl and cl[0][1] == "SyncServiceImpl" and cl[0][2] == "saveKeeping"


def test_missing_looks_in_that_class(tmp_path):
    d = tmp_path / "src" / "main" / "java" / "com" / "acme" / "sync"
    d.mkdir(parents=True)
    (d / "SyncServiceImpl.java").write_text("package com.acme.sync;\nclass SyncServiceImpl {}\n")
    (d / "Other.java").write_text(
        "package com.acme.sync;\nclass Other { Mono<Void> saveKeeping(Settings incoming, String c) { return null; } }\n")
    gaps = tc.missing(REQ, str(tmp_path))
    assert gaps and "saveKeeping" in gaps[0]                       # declared elsewhere does not count
    (d / "SyncServiceImpl.java").write_text(
        "package com.acme.sync;\nclass SyncServiceImpl {\n"
        " Mono<Void> saveKeeping(Settings incoming, String collectionName) { return null; }\n}\n")
    assert tc.missing(REQ, str(tmp_path)) == []


def test_no_owner_without_an_on_clause():
    assert tc.claims("Add `int total(List<Item> items)` to the cart.")[0][1] is None
