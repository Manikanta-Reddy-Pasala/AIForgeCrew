"""The request's own example values must be in the turn's tests (click a1:
the exact zsh output and the `_CLI_COMPLETE=complete_bash` text the request
gave were never tested, and both were wrong: 3/17 on that hidden file)."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _example_gate as E

_REQ = ("Add shell completion and add pytest tests for it.\n"
        "- `[] \"\"` -> `[]`; `[] \"-\"` -> `[\"-t\", \"--test\", \"--help\"]`.\n"
        "- The script contains `_CLI_COMPLETE=complete_bash`; the output is "
        "`\"plain\\na\\n_\\nplain\\nb\\nbee\\n\"`; help `\"_\"` when empty; "
        "format `\"{type}\\n{value}\"`; returns `0`.\n")


def test_example_values_are_read_and_templates_are_not():
    assert E.examples(_REQ) == ['["-t", "--test", "--help"]', "_CLI_COMPLETE=complete_bash",
                                '"plain\\na\\n_\\nplain\\nb\\nbee\\n"']


def _repo(tmp_path, test_text):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pkg.py").write_text("x = 1\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True, env=env)
    sha = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True,
                         text=True, env=env).stdout.strip()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_new.py").write_text(test_text)
    return SimpleNamespace(goal=_REQ, cwd=str(tmp_path), edits_made=2, head0=sha)


def test_untested_examples_are_named_and_tested_ones_are_not(tmp_path):
    st = _repo(tmp_path, "def test_a():\n    assert f([], '-') == ['-t', '--test', '--help']\n"
                         "def test_b():\n    assert f([], '') == []\n")
    assert E.untested(st) == ["_CLI_COMPLETE=complete_bash", '"plain\\na\\n_\\nplain\\nb\\nbee\\n"']


def test_an_input_and_its_result_must_meet_in_one_test(tmp_path):
    """click a7: `[] ""` -> `[]` untested; the value `[]` alone is everywhere."""
    st = _repo(tmp_path, "def test_a():\n    assert f([], '-') == ['-t', '--test', '--help']\n"
                         "def test_b():\n    assert f([], '-x') == []\n"
                         "def test_c():\n    assert g('') == ['x']\n")
    got = E.untested(st)
    assert got[0] == '[] ""` -> `[]' and not any("--test" in v for v in got)
    assert "exactly that input" in E.message(got)


def test_the_input_is_not_the_result_and_a_docstring_is_no_empty_word():
    w = E._windows('def test_a():\n    """doc"""\n    assert f([], "-x") == []\n')
    assert not E._pair_held('[] ""', "[]", w)
    w = E._windows('def test_a():\n    assert f([], "") == ["--help"]\n')
    assert not E._pair_held('[] ""', "[]", w)                    # the wrong result
    w = E._windows('def test_a():\n    words = f([], "")\n    assert words == []\n')
    assert E._pair_held('[] ""', "[]", w)
    w = E._windows('@pytest.mark.parametrize("a,i,want", [\n    ([], "-", ["-t"]),\n'
                   '    (["x"], "", []),\n])\ndef test_p(a, i, want):\n    pass\n')
    assert E._pair_held('[] "-"', '["-t"]', w)
    assert E._parts('"a\\" b" x') == ['"a\\"b"', "x"]
    w = E._windows('@pytest.mark.parametrize("v,want", [("a \'b c\'", ["a", "b c"])])\n'
                   'def test_s(v, want):\n    assert split_arg_string(v) == want\n')
    assert not E._pair_held('split_arg_string("a \'b c\'")', '["a", "b c"]', w)   # name in another block
    w = E._windows('def test_s():\n    v = "a \'b c\'"\n    assert split_arg_string(v) == ["a", "b c"]\n')
    assert E._pair_held('split_arg_string("a \'b c\'")', '["a", "b c"]', w)


def test_pair_parts_are_split_at_the_top_level():
    assert E._parts('["-t", "a"] "-"') == ['["-t","a"]', '"-"']
    assert E._parts("[] \"\"") == ["[]", '""']
    assert E.pairs("`a` -> `'b'`; it was `c` -> `'d'` before") == [("a", "'b'")]
    assert E.pairs("`get_args()` -> `(args, incomplete)`; `f()` -> `[]`; `n` -> `3`") == [
        ("f()", "[]"), ("n", "3")]


def test_the_gate_sends_back_once(tmp_path):
    st = _repo(tmp_path, "def test_a():\n    pass\n")
    st.convo = []
    g = E.gate(st, {"text": "done"})
    out = []
    try:
        while True:
            out.append(next(g))
    except StopIteration as e:
        assert e.value == "continue"
    assert "_CLI_COMPLETE=complete_bash" in st.convo[-1]["content"]
    g = E.gate(st, {"text": "done"})
    try:
        next(g)
    except StopIteration as e:
        assert e.value is None


def test_no_tests_asked_or_none_written_says_nothing(tmp_path):
    st = _repo(tmp_path, "")
    st.goal = _REQ.replace("and add pytest tests for it", "").replace("test", "")
    assert E.untested(st) == []
    st = SimpleNamespace(goal=_REQ, cwd=str(tmp_path / "nowhere"), edits_made=2, head0="abc")
    assert E.untested(st) == []
    st = _repo(tmp_path / "c", "def test_a():\n    pass\n")
    st.head0 = "0" * 40                                      # a base git cannot read
    assert E.untested(st) == []


def test_a_long_unclosed_escaped_span_does_not_hang():
    import time
    t0 = time.monotonic()
    E.examples("add tests: `\"" + "\\n" * 90 + "x` ")
    assert time.monotonic() - t0 < 1


def test_do_not_add_tests_and_earlier_untracked_files_say_nothing(tmp_path):
    st = _repo(tmp_path, "def test_a():\n    pass\n")
    st.goal = _REQ + "\nDo not add new tests."
    assert E.untested(st) == []
    st.goal = _REQ
    st.example_untracked0 = {"tests/test_new.py"}          # was there before the turn
    assert E.untested(st) == []


def test_a_currently_sentence_is_no_expectation_and_loose_forms_match(tmp_path):
    req = ("Fix it and add tests. Currently it prints `\"Error: bad thing\"`; "
           "with `_X_MODE=fast` the output is `[\"a\", \"b\"]`.")
    assert E.examples(req) == ["_X_MODE=fast", '["a", "b"]']
    assert E.examples(req, expected_only=True) == ['["a", "b"]']
    st = _repo(tmp_path, 'def test_a(monkeypatch):\n    monkeypatch.setenv("_X_MODE", "fast")\n'
                         '    assert run() == [\n        "a",\n        "b",\n    ]\n')
    st.goal = req
    assert E.untested(st) == []


def test_halfway_with_no_test_written_names_every_example(tmp_path):
    st = _repo(tmp_path, "")
    (tmp_path / "tests" / "test_new.py").unlink()
    assert E.untested(st) == []
    got = E.untested(st, unwritten=True)
    assert got and got[0] == '[] ""` -> `[]'


def test_halfway_silent_when_tests_are_only_mentioned(tmp_path):
    st = _repo(tmp_path, "")
    (tmp_path / "tests" / "test_new.py").unlink()
    st.goal = 'Fix the parser; it fails with the existing tests: `[] ""` -> `[]`.'
    assert E.untested(st, unwritten=True) == []
