"""A failure fixed through backgrounded runs is remembered.

Live case: on a real PosClientBackend chain the fixes store stayed empty
although sessions went red -> green: every `mvn test` was backgrounded (its
end came via command_wait, which the hook ignored) and `mvn -q` prints
nothing when green (no runner "passed" text).
"""
from types import SimpleNamespace

from aiforge_core.runtime import cmd_finished, ops_memory

RED = ("[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0\n"
       "[ERROR]   FooTest.adds:12 expected: <3> but was: <4>\n[INFO] BUILD FAILURE")


def test_background_red_then_quiet_green_records_the_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    finals = {"bg-1": {"ok": False, "code": 1, "output": RED, "cmd": "mvn -o -q test"},
              "bg-2": {"ok": True, "code": 0, "output": "", "cmd": "mvn -o -q test"}}
    monkeypatch.setattr(cmd_finished, "get", lambda i: finals.get(i))
    st = SimpleNamespace(file_hashes={"src/Foo.java": "a"})
    start = {"ok": True, "id": "bg-1", "running": True}
    assert ops_memory.fix_hook(st, "run_command", {"cmd": "mvn -o -q test"}, start, str(tmp_path)) == ""
    ops_memory.fix_hook(st, "command_wait", {"id": "bg-1"},
                        {"ok": False, "id": "bg-1", "running": False, "code": 1}, str(tmp_path), "Pcb")
    assert st.fix_open                                   # the failure is open
    st.file_hashes = {"src/Foo.java": "b"}               # the fix landed
    ops_memory.fix_hook(st, "command_wait", {"id": "bg-2"},
                        {"ok": True, "id": "bg-2", "running": False, "code": 0}, str(tmp_path), "Pcb")
    fixes = ops_memory.load()["fixes"]
    assert len(fixes) == 1
    e = next(iter(fixes.values()))
    assert e["signature"] == "junit:FooTest.adds" and e["files"] == ["src/Foo.java"]
    # the same failure later in another session: the note comes back
    st2 = SimpleNamespace(file_hashes={})
    note = ops_memory.fix_hook(st2, "run_command", {"cmd": "mvn test"},
                               {"ok": False, "output": RED}, str(tmp_path), "Pcb")
    assert "seen before" in note and "src/Foo.java" in note


def test_hunk_names_across_languages():
    from aiforge_core.runtime.ops_memory import _hunk_name
    assert _hunk_name("public final class InventoryAgingCalculator {") == "InventoryAgingCalculator"
    assert _hunk_name("    public List<String> labels() {") == "labels"
    assert _hunk_name("static String txnDay(Document doc) {") == "txnDay"
    assert _hunk_name("def test_repr(self):") == "test_repr"
    assert _hunk_name("pub fn matches(&self, v: &Version) -> bool {") == "matches"
    assert _hunk_name("impl VersionReq {") == "VersionReq"
    assert _hunk_name("bool XMLDocument::Parse(const char* p)") == "Parse"
