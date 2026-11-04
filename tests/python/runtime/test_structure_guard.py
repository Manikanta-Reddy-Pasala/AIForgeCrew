"""An edit that moves or drops code it never named is reported at once.

On click a new method's end swallowed the next one (``invoke`` became the
new method's body); syntax and error checks passed and the model spent 25
minutes debugging a file it had broken a step earlier."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _structure_guard as G

BEFORE = '''class Multi:
    def parse(self):
        return 1

    def invoke(self, ctx):
        def _process(v):
            return v
        return _process(ctx)
'''

# shell_complete inserted with its body running on into invoke's
SWALLOWED = '''class Multi:
    def parse(self):
        return 1

    def shell_complete(self, ctx):
        items = []
        return items

        def invoke(self, ctx):
            def _process(v):
                return v
            return _process(ctx)
'''


def test_a_method_swallowed_by_a_new_one_is_named_with_where_it_went():
    # the realistic insert: anchored on invoke's own def line
    lost = G.lost_names(BEFORE, SWALLOWED, "    def invoke(self, ctx):")
    names = [q for q, _ in lost]
    assert "Multi.invoke" in names
    now = dict(lost)["Multi.invoke"]
    assert now[0] == "Multi.shell_complete.invoke"


def test_a_method_the_old_text_names_may_be_removed():
    after = BEFORE.replace("    def invoke(self, ctx):\n        def _process(v):\n"
                           "            return v\n        return _process(ctx)\n", "")
    assert G.lost_names(BEFORE, after, "    def invoke(self, ctx):") == []


def test_a_longer_name_in_the_old_text_does_not_excuse_a_lost_one():
    after = BEFORE.replace("    def parse(self):\n        return 1\n\n", "")
    assert [q for q, _ in G.lost_names(BEFORE, after, "def parse_args(x):")] == ["Multi.parse"]


def test_an_insert_that_keeps_every_definition_says_nothing():
    after = BEFORE.replace("    def invoke", "    def extra(self):\n        return 2\n\n"
                           "    def invoke")
    assert G.lost_names(BEFORE, after, "    def invoke(self, ctx):") == []


@pytest.mark.parametrize("name,args", [
    ("file_write", {"path": "core.py", "content": ""}),
    ("rename_symbol", {"path": "core.py", "name": "invoke", "new_name": "run"}),
    ("format", {"path": "core.py"}),
    ("editor", {"command": "undo_edit", "path": "core.py"}),
])
def test_calls_that_restructure_on_purpose_are_not_checked(tmp_path, name, args):
    (tmp_path / "core.py").write_text(BEFORE)
    st = SimpleNamespace(convo=[])
    G.snapshot(st, name, args, str(tmp_path))
    assert st.pre_edit == {}


def test_old_text_for_another_file_does_not_excuse_this_one(tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("def foo():\n    return 1\n")
    b.write_text("def foo():\n    return 1\n\n\ndef bar():\n    return 2\n")
    args = {"edits": [{"path": "a.py", "old_str": "def foo():", "new_str": "def foo2():"},
                      {"path": "b.py", "old_str": "return 2", "new_str": "return 3"}]}
    st = SimpleNamespace(convo=[])
    G.snapshot(st, "multi_edit", args, str(tmp_path))
    a.write_text("def foo2():\n    return 1\n")
    b.write_text("def bar():\n    return 3\n")
    ev = list(G.check(st, "multi_edit", [("a.py", str(a)), ("b.py", str(b))], args,
                      str(tmp_path)))
    assert len(ev) == 1 and "b.py" in ev[0]["text"] and "`foo`" in ev[0]["text"]


def test_a_broken_file_is_left_to_the_syntax_check():
    assert G.lost_names(BEFORE, "class Multi(:\n", "") == []


def test_the_check_runs_on_the_landed_edit_and_tells_the_model(tmp_path):
    f = tmp_path / "core.py"
    f.write_text(BEFORE)
    st = SimpleNamespace(convo=[])
    args = {"path": "core.py", "old_text": "        return 1\n", "new_text": "x"}
    G.snapshot(st, "file_patch", args, str(tmp_path))
    f.write_text(SWALLOWED)
    ev = list(G.check(st, "file_patch", [("core.py", str(f))], args, str(tmp_path)))
    assert ev and "Multi.invoke" in ev[0]["text"]
    assert "structure check" in st.convo[-1]["content"]
    assert st.pre_edit == {}


def test_the_check_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STRUCTURE_CHECK", "0")
    f = tmp_path / "core.py"
    f.write_text(BEFORE)
    st = SimpleNamespace(convo=[])
    G.snapshot(st, "file_patch", {"path": "core.py"}, str(tmp_path))
    f.write_text(SWALLOWED)
    assert list(G.check(st, "file_patch", [("core.py", str(f))], {})) == []
