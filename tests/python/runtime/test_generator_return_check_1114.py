"""AF003: `return <call>` in a function that also yields (post-edit error check)."""
from __future__ import annotations

from aiforge_core.runtime import post_edit as P

BUG = '''
def running_min(iterable, *, maxlen=None):
    if maxlen is not None:
        return _windowed(iterable, maxlen)
    for item in iterable:
        yield item
'''


def _check(tmp_path, src):
    f = tmp_path / "m.py"
    f.write_text(src)
    return P._generator_returns(str(f))


def test_a_returned_call_next_to_a_yield_is_reported(tmp_path):
    errs = _check(tmp_path, BUG)
    assert len(errs) == 1 and "AF003" in errs[0] and ":4:" in errs[0]
    assert "yield from" in errs[0] and "running_min" in errs[0]


def test_only_code_written_this_turn_is_reported(tmp_path):
    assert P.new_generator_returns("m.py", BUG, BUG) == []           # already in the repo
    errs = P.new_generator_returns("m.py", "def other():\n    pass\n", BUG)
    assert len(errs) == 1 and "AF003" in errs[0]
    assert len(P.new_generator_returns("m.py", "", BUG)) == 1        # a new file


def test_it_reaches_the_model_through_the_error_check(tmp_path):
    import subprocess
    from types import SimpleNamespace
    from aiforge_core.runtime.chat_agent._turn import _post_edit as T
    git = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                                    capture_output=True)
    f = tmp_path / "m.py"
    f.write_text("def running_min(iterable):\n    for item in iterable:\n        yield item\n")
    git("init", "-q"); git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    f.write_text(BUG)
    st = SimpleNamespace(cwd=str(tmp_path), head0=base)
    events = list(T.post_edit_diagnostics(st, [("m.py", str(f))], str(tmp_path)))
    assert events and any("AF003" in n for n in st.held_notes)   # the note the model reads


def test_legitimate_shapes_are_left_alone(tmp_path):
    ok = '''
def plain(x):
    return helper(x)

def gen(x):
    if not x:
        return
    yield from helper(x)
    return finish(x)          # PEP 380: a yield-from generator may return a value

def outer(x):
    def inner():
        yield 1
    return list(inner())

def early(x):
    if x is None:
        return None
    yield x

def driver(x):
    for step in x:
        yield step
    return summary(x)         # read by the caller's `yield from`

class C:
    def m(self):
        cb = lambda: (yield)
        return build(cb)
'''
    assert _check(tmp_path, ok) == []


def test_a_file_that_does_not_parse_is_skipped(tmp_path):
    assert _check(tmp_path, "def broken(:\n") == []
