"""The calls a request says the test mocks must be the calls the new code makes."""
import subprocess

from aiforge_core.runtime import mock_contract as mc

REQ = ("The unit test will call: package-private `Mono<Void> saveKeeping(com.acme.Settings incoming, "
       "String collectionName)` on `com.acme.SyncService`, with its `ReactiveMongoTemplate mongoTemplate` "
       "field mocked for `findById(id, org.bson.Document.class, collectionName)`, `getConverter()` and "
       "`save(org.bson.Document, collectionName)`.\n\nFix it with a unit test.")


def _repo(tmp_path, body):
    src = tmp_path / "src/main/java/com/acme"
    src.mkdir(parents=True)
    f = src / "SyncService.java"
    f.write_text("class SyncService {\n}\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True)
    f.write_text("class SyncService {\n" + body + "\n}\n")
    t = tmp_path / "src/test/java/com/acme"
    t.mkdir(parents=True)
    (t / "SyncServiceTest.java").write_text("x(getConverter()); findById(a, Document.class, b);")
    return str(tmp_path)


def test_claims_are_the_mocked_calls_only():
    got = [(n, c) for _, n, c in mc.claims(REQ)]
    assert got == [("findById", ["Document"]), ("getConverter", []), ("save", [])]
    assert mc.claims("Add `save(x)` to the service.") == []


def test_wrong_class_and_missing_call_are_gaps(tmp_path):
    root = _repo(tmp_path, "Mono<Void> saveKeeping(Settings in, String c) {\n"
                           "  return t.findById(in.getId(), Settings.class, c).flatMap(e -> t.save(in, c)).then();\n}")
    gaps = mc.missing(REQ, root)
    assert len(gaps) == 2
    assert "Document.class" in gaps[0] and "getConverter" in gaps[1]


def test_code_that_makes_the_mocked_calls_passes(tmp_path):
    root = _repo(tmp_path, "Mono<Void> saveKeeping(Settings in, String c) {\n"
                           "  return t.findById(in.getId(), Document.class, c).flatMap(e -> {\n"
                           "    Document d = new Document(); t.getConverter().write(in, d);\n"
                           "    return t.save(d, c); }).then();\n}")
    assert mc.missing(REQ, root) == []


def test_off_switch(tmp_path, monkeypatch):
    root = _repo(tmp_path, "void x() {}")
    monkeypatch.setenv("AIFORGE_CHAT_MOCK_CONTRACT", "0")
    assert mc.missing(REQ, root) == []
