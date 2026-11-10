"""The harness notices an edit that changes what an existing test checks,
across frameworks, and stays quiet on refactors, formatting and new tests."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _test_guard as G


@pytest.mark.parametrize("path,is_test", [
    ("tests/test_fields.py", True), ("test_x.py", True), ("pkg/x_test.py", True),
    ("api/handler_test.go", True), ("src/test/java/a/FooTest.java", True),
    ("app/FooTests.cs", True), ("spec/models/user_spec.rb", True),
    ("web/src/Button.test.tsx", True), ("web/src/util.spec.js", True),
    ("src/__tests__/a.js", True), ("tests/PhpUnitTest.php", True),
    ("src/marshmallow/fields.py", False), ("src/latest.py", False),
    ("docs/testing.md", False), ("src/contest.go", False),
])
def test_test_files_are_found_in_every_layout(path, is_test):
    assert bool(G._TEST_FILE.search(path)) is is_test


@pytest.mark.parametrize("before,after", [
    # pytest: the marshmallow case
    ('    assert repr(f) == "<fields.Raw(a=1)>"\n', '    assert repr(f) == "<fields.Raw(a=1, b=[])>"\n'),
    # unittest
    ("        self.assertEqual(f(2), 4)\n", "        self.assertEqual(f(2), 5)\n"),
    # pytest.raises match
    ('    with pytest.raises(ValueError, match="bad"):\n', "    with pytest.raises(ValueError):\n"),
    # jest
    ("  expect(sum(1, 2)).toBe(3);\n", "  expect(sum(1, 2)).toBe(4);\n"),
    # go
    ('\t\tt.Errorf("got %v", got)\n', "\n"),
    ("\trequire.Equal(t, 3, got)\n", "\trequire.Equal(t, 4, got)\n"),
    # junit
    ("        assertEquals(3, calc.add(1, 2));\n", "        assertEquals(4, calc.add(1, 2));\n"),
    ("        assertThat(list).hasSize(2);\n", "        assertThat(list).hasSize(3);\n"),
    # c#
    ("        Assert.Equal(3, Calc.Add(1, 2));\n", "        Assert.Equal(4, Calc.Add(1, 2));\n"),
    # rust
    ("    assert_eq!(add(1, 2), 3);\n", "    assert_eq!(add(1, 2), 4);\n"),
    # rspec / minitest
    ("    expect(user.name).to eq('a')\n", "    expect(user.name).to eq('b')\n"),
    ("    assert_equal 3, add(1, 2)\n", "    assert_equal 4, add(1, 2)\n"),
    # php
    ("        $this->assertSame(3, add(1, 2));\n", "        $this->assertSame(4, add(1, 2));\n"),
    # commented out
    ("    assert f() == 1\n", "    # assert f() == 1\n"),
    # the marshmallow edit: a multi-line expected value, assert line unchanged
    ('    assert repr(field) == (\n        "<fields.Raw("\n        f"e={m})>"\n    )\n',
     '    assert repr(field) == (\n        "<fields.Raw("\n        f"e={m}, "\n        "pre_load=[])>"\n    )\n'),
    # java split over lines
    ("        assertEquals(\n            3,\n            calc.add(1, 2));\n",
     "        assertEquals(\n            4,\n            calc.add(1, 2));\n"),
    # whole test deleted
    ("def test_a():\n    assert f() == 1\n", ""),
])
def test_a_changed_or_removed_assertion_is_reported(before, after):
    assert G.lost_assertions(before, after)


@pytest.mark.parametrize("before,after", [
    ("def test_a():\n    pass\n", "@pytest.mark.skip\ndef test_a():\n    pass\n"),
    ("def test_a():\n    pass\n", "@pytest.mark.xfail(reason='x')\ndef test_a():\n    pass\n"),
    ("it('adds', () => {});\n", "it.skip('adds', () => {});\n"),
    ("it('adds', () => {});\n", "xit('adds', () => {});\n"),
    ("func TestA(t *testing.T) {\n", "func TestA(t *testing.T) {\n\tt.Skip(\"later\")\n"),
    ("    @Test\n    void a() {}\n", "    @Disabled\n    @Test\n    void a() {}\n"),
    ("#[test]\nfn a() {}\n", "#[test]\n#[ignore]\nfn a() {}\n"),
])
def test_a_new_skip_is_reported(before, after):
    assert any(x.startswith("(new skip)") for x in G.lost_assertions(before, after))


@pytest.mark.parametrize("before,after", [
    # tests moved / reordered
    ("def test_a():\n    assert a() == 1\n\ndef test_b():\n    assert b() == 2\n",
     "def test_b():\n    assert b() == 2\n\ndef test_a():\n    assert a() == 1\n"),
    # formatter: quotes, spacing, trailing comma
    ("    assert f('x') == {'a': 1}\n", '    assert f("x") == {"a": 1}\n'),
    ("    assert f( 1,2 ) == 3\n", "    assert f(1, 2) == 3\n"),
    ("    self.assertEqual(\n        f(1), 2,\n    )\n", "    self.assertEqual(\n        f(1), 2\n    )\n"),
    # only new tests added
    ("def test_a():\n    assert a() == 1\n",
     "def test_a():\n    assert a() == 1\n\ndef test_new():\n    assert new() == 2\n"),
    # an existing skip kept as is
    ("@pytest.mark.skip\ndef test_a():\n    assert 1\n", "@pytest.mark.skip\ndef test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n"),
])
def test_refactors_formatting_and_new_tests_are_quiet(before, after):
    assert G.lost_assertions(before, after) == []


def _repo(tmp_path, rel, text):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "seed"], check=True)
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return f, head


def _run(st, f, tmp_path, monkeypatch):
    held = []
    from aiforge_core.runtime.chat_agent._turn import _notes
    monkeypatch.setattr(_notes, "hold", lambda st, text: held.append(text))
    events = list(G.check(st, "file_patch", [(str(f), str(f))], {}, str(tmp_path)))
    return events, held


def test_the_marshmallow_edit_gets_one_note(tmp_path, monkeypatch):
    f, head = _repo(tmp_path, "tests/test_fields.py",
                    'def test_repr():\n    assert repr(f) == "<fields.Raw(a=1)>"\n')
    st = SimpleNamespace(cwd=str(tmp_path), head0=head)
    f.write_text('def test_repr():\n    assert repr(f) == "<fields.Raw(a=1, pre_load=[])>"\n')
    events, held = _run(st, f, tmp_path, monkeypatch)
    assert events and len(held) == 1
    assert "Put the assertion back" in held[0] and "test_fields.py" in held[0]
    events, held = _run(st, f, tmp_path, monkeypatch)       # still gone: one reminder
    assert not events and len(held) == 1 and "still not back" in held[0]
    assert _run(st, f, tmp_path, monkeypatch) == ([], [])  # then quiet


def test_new_test_files_and_source_files_are_never_checked(tmp_path, monkeypatch):
    f, head = _repo(tmp_path, "src/calc.py", "assert True\n")
    st = SimpleNamespace(cwd=str(tmp_path), head0=head)
    f.write_text("x = 1\n")
    assert _run(st, f, tmp_path, monkeypatch) == ([], [])
    new = tmp_path / "tests" / "test_new.py"
    new.parent.mkdir()
    new.write_text("def test_n():\n    assert 1\n")
    assert _run(st, new, tmp_path, monkeypatch) == ([], [])


def test_off_switch(tmp_path, monkeypatch):
    f, head = _repo(tmp_path, "tests/test_a.py", "def test_a():\n    assert f() == 1\n")
    st = SimpleNamespace(cwd=str(tmp_path), head0=head)
    f.write_text("def test_a():\n    assert f() == 2\n")
    monkeypatch.setenv("AIFORGE_CHAT_TEST_GUARD", "0")
    assert _run(st, f, tmp_path, monkeypatch) == ([], [])


@pytest.mark.parametrize("goal,allowed", [
    ("Add pre_load and post_load to Field. Keep the existing test suite passing.", False),
    ("Fix the crash in parse_date when the day is missing.", False),
    ("Add running_statistics. Add tests for the new behaviour.", False),
    ("Replace it ... keeping the existing test suite working (except the tests for the "
     "old `_bashcomplete` module, which you may delete together with it).", True),
    ("Rename `fetch` to `load` everywhere and update the tests.", True),
    ("Change the default timeout to 30s; the tests that check 10s should be updated.", True),
    ("The repr must now include pre_load; adjust test_fields.py accordingly.", True),
    ("`tests/test_basic.py::test_boolean_conversion` fails. Find the bug in the library "
     "and fix it; do not change the test.", False),
    ("Rename `name_sequence` to `make_name_generator`, and update every use of it in "
     "`sqlglot/` and `tests/`. Behaviour must not change.", True),
    ("Speed up the parser without touching the tests.", False),
    ("Make the build faster. Don't modify any tests.", False),
    ("Add a --json flag to the CLI and add tests for it.", False),
    ("## 5. Existing tests to update\nThe old trigger tests expect a Command node.", True),
    ("Keep the existing tests working (don't break the existing tests).", False),
    ("Refactor utils.py into a package; the test imports may need to be updated.", True),
])
def test_the_request_decides_how_firm_the_note_is(goal, allowed):
    assert G.allows_test_changes(goal, "tests/test_fields.py") is allowed


def test_an_allowed_change_gets_the_soft_note(tmp_path, monkeypatch):
    f, head = _repo(tmp_path, "tests/test_a.py", "def test_a():\n    assert f() == 1\n")
    st = SimpleNamespace(cwd=str(tmp_path), head0=head,
                         goal="Change f to return 2 and update the tests.")
    f.write_text("def test_a():\n    assert f() == 2\n")
    _, held = _run(st, f, tmp_path, monkeypatch)
    assert held and "allows some existing tests to change" in held[0]


def test_a_looser_rewrite_gets_one_firmer_reminder(tmp_path, monkeypatch):
    f, head = _repo(tmp_path, "tests/test_a.py",
                    'def test_r():\n    assert repr(x) == "<X(a=1)>"\n')
    st = SimpleNamespace(cwd=str(tmp_path), head0=head, goal="Add b to X.")
    f.write_text('def test_r():\n    assert repr(x) == "<X(a=1, b=2)>"\n')
    _, held = _run(st, f, tmp_path, monkeypatch)
    assert len(held) == 1
    f.write_text('def test_r():\n    assert "a=1" in repr(x)\n')
    _, held = _run(st, f, tmp_path, monkeypatch)
    assert len(held) == 1 and "still not back" in held[0]
    _, held = _run(st, f, tmp_path, monkeypatch)
    assert held == []                                     # once


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_TEST_GUARD_GATE", "1")


def _gate(st):
    g = G.gate(st, {"text": ""})
    try:
        while True:
            next(g)
    except StopIteration as stop:
        return stop.value


def test_the_finish_gate_sends_a_weakened_test_back_once(tmp_path):
    f, head = _repo(tmp_path, "tests/test_a.py", "def test_a():\n    assert f() == 1\n")
    st = SimpleNamespace(cwd=str(tmp_path), head0=head, goal="Add g().", edits_made=2, convo=[])
    f.write_text("def test_a():\n    assert f() in (1, 2)\n")
    assert _gate(st) == "continue"
    msg = st.convo[-1]["content"]
    assert "-    assert f() == 1" in msg and "+    assert f() in (1, 2)" in msg
    assert _gate(st) is None                              # no new edit: not again
    st.edits_made = 3
    f.write_text("def test_a():\n    assert f() >= 1\n")
    assert _gate(st) == "continue" and "second time" in st.convo[-1]["content"]
    st.edits_made = 4
    assert _gate(st) is None                              # two at most


def test_the_finish_gate_lets_allowed_or_restored_tests_through(tmp_path):
    f, head = _repo(tmp_path, "tests/test_a.py", "def test_a():\n    assert f() == 1\n")
    st = SimpleNamespace(cwd=str(tmp_path), head0=head, edits_made=1, convo=[],
                         goal="Change f to return 2 and update the tests.")
    f.write_text("def test_a():\n    assert f() == 2\n")
    assert _gate(st) is None
    st.goal = "Add g()."
    f.write_text("def test_a():\n    assert f() == 1\n\ndef test_g():\n    assert g()\n")
    assert _gate(st) is None


def test_the_finish_gate_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_TEST_GUARD_GATE", raising=False)
    f, head = _repo(tmp_path, "tests/test_a.py", "def test_a():\n    assert f() == 1\n")
    st = SimpleNamespace(cwd=str(tmp_path), head0=head, goal="Add g().", edits_made=2, convo=[])
    f.write_text("def test_a():\n    assert f() == 2\n")
    assert _gate(st) is None


def test_the_answer_facts_name_the_changed_test(tmp_path):
    from aiforge_core.runtime import turn_facts_line as F
    f, head = _repo(tmp_path, "tests/test_a.py", "def test_a():\n    assert f() == 1\n")
    f.write_text("def test_a():\n    assert f() == 2\n")
    assert F._weakened_tests(str(tmp_path), head) == \
        "existing test assertions changed: `tests/test_a.py` (1)"
    f.write_text("def test_a():\n    assert f() == 1\n\ndef test_b():\n    assert 2\n")
    assert F._weakened_tests(str(tmp_path), head) == ""


@pytest.mark.parametrize("goal,asks", [
    ("Add a --json flag to the CLI.", False),
    ("Fix the crash in load_csv when qty is missing.", False),
    ("Rename low_stock to below_threshold everywhere.", False),
    ("Add a --json flag and write tests for it.", True),
    ("Add pytest tests for Inventory.find.", True),
    ("Add the flag, with unit tests.", True),
    ("Add the flag; do not write tests.", False),
])
def test_asks_for_tests(goal, asks):
    assert G.asks_for_tests(goal) is asks


def _new_test_run(tmp_path, monkeypatch, goal):
    f, head = _repo(tmp_path, "src/app.py", "x = 1\n")
    new = tmp_path / "tests" / "test_app.py"
    new.parent.mkdir()
    new.write_text("def test_x():\n    assert True\n")
    st = SimpleNamespace(cwd=str(tmp_path), head0=head, goal=goal)
    held = []
    from aiforge_core.runtime.chat_agent._turn import _notes
    monkeypatch.setattr(_notes, "hold", lambda st, text: held.append(text))
    events = list(G.unasked_new_tests(st, [(str(new), str(new))], str(tmp_path)))
    return st, events, held, new


def test_a_new_test_file_nobody_asked_for_gets_one_note(tmp_path, monkeypatch):
    st, events, held, new = _new_test_run(tmp_path, monkeypatch, "Add a --json flag to the CLI.")
    assert events and len(held) == 1 and "did not ask for tests" in held[0]
    assert list(G.unasked_new_tests(st, [(str(new), str(new))], str(tmp_path))) == []


def test_asked_for_tests_or_an_existing_test_file_is_quiet(tmp_path, monkeypatch):
    _, events, held, _ = _new_test_run(tmp_path, monkeypatch, "Add a flag and write tests for it.")
    assert not events and not held
    f, head = _repo(tmp_path / "b", "tests/test_a.py", "def test_a():\n    assert 1\n")
    f.write_text("def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n")
    st = SimpleNamespace(cwd=str(tmp_path / "b"), head0=head, goal="Rename a to b.")
    assert list(G.unasked_new_tests(st, [(str(f), str(f))], str(tmp_path / "b"))) == []
