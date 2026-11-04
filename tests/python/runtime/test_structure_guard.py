"""An edit that moves or drops code it never named is reported at once.

On click a new method's end swallowed the next one (``invoke`` became the
new method's body); syntax and error checks passed and the model spent 25
minutes debugging a file it had broken a step earlier."""
from __future__ import annotations

from types import SimpleNamespace

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
    lost = G.lost_names(BEFORE, SWALLOWED, "    def parse(self):\n        return 1\n", False)
    names = [q for q, _ in lost]
    assert "Multi.invoke" in names
    now = dict(lost)["Multi.invoke"]
    assert now[0] == "Multi.shell_complete.invoke"


def test_a_method_the_old_text_names_may_be_removed():
    after = BEFORE.replace("    def invoke(self, ctx):\n        def _process(v):\n"
                           "            return v\n        return _process(ctx)\n", "")
    assert G.lost_names(BEFORE, after, "    def invoke(self, ctx):", False) == []


def test_an_insert_that_keeps_every_definition_says_nothing():
    after = BEFORE.replace("    def invoke", "    def extra(self):\n        return 2\n\n"
                           "    def invoke")
    assert G.lost_names(BEFORE, after, "    def invoke(self, ctx):", False) == []


def test_a_whole_file_rewrite_reports_moves_but_not_removals():
    gone = "class Multi:\n    def parse(self):\n        return 1\n"
    assert G.lost_names(BEFORE, gone, "", True) == []
    assert [q for q, _ in G.lost_names(BEFORE, SWALLOWED, "", True)] == ["Multi.invoke"]


def test_a_broken_file_is_left_to_the_syntax_check():
    assert G.lost_names(BEFORE, "class Multi(:\n", "", False) == []


def test_the_check_runs_on_the_landed_edit_and_tells_the_model(tmp_path):
    f = tmp_path / "core.py"
    f.write_text(BEFORE)
    st = SimpleNamespace(convo=[])
    args = {"path": "core.py", "old_text": "        return 1\n", "new_text": "x"}
    G.snapshot(st, "file_patch", args, str(tmp_path))
    f.write_text(SWALLOWED)
    ev = list(G.check(st, "file_patch", [("core.py", str(f))], args))
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
