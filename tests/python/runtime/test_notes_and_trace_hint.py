"""Harness notes reach the model after the step's result, and a test that keeps
failing after a fix gets a trace of which added lines ran."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._native_replay import to_native_messages
from aiforge_core.runtime.chat_agent._turn import _notes, _trace_hint


def _st(native=True, **kw):
    return SimpleNamespace(native_on=native, convo=[], **kw)


# ── notes: held during a step, sent after its result ─────────────────────

def test_a_note_held_during_a_step_follows_its_result_as_its_own_message():
    st = _st()
    st.convo = [{"role": "assistant", "content": 'ACTION: file_patch\nARGS_JSON: {"path": "a.py"}'}]
    _notes.hold(st, "[automated error check — not the user] a.py: F821 undefined name x")
    assert len(st.convo) == 1                         # nothing before the result
    st.convo.append({"role": "user", "content": "OBSERVATION: {\"ok\": true}"})
    _notes.flush(st)
    out = to_native_messages(st.convo)
    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    assert out[1]["content"] == '{"ok": true}'        # the edit's real result, not "not run"
    assert "undefined name x" in out[2]["content"]


def test_two_notes_merge_and_text_mode_rides_on_the_result():
    st = _st()
    st.convo = [{"role": "user", "content": "OBSERVATION: x"}]
    _notes.send(st, "[harness — not the user] one")
    _notes.send(st, "[automated error check — not the user] two")
    assert len(st.convo) == 2 and "one" in st.convo[1]["content"] and "two" in st.convo[1]["content"]
    st = _st(native=False)
    st.convo = [{"role": "user", "content": "OBSERVATION: x"}]
    _notes.send(st, "[harness — not the user] one")
    assert len(st.convo) == 1 and st.convo[0]["content"].endswith("one")


def test_the_post_edit_check_holds_its_note(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _post_edit
    from aiforge_core.runtime import post_edit
    f = tmp_path / "a.py"
    f.write_text("y = x\n")
    monkeypatch.setattr(post_edit, "diagnostics", lambda full, cwd: ["a.py:1:5: F821 Undefined name `x`"])
    monkeypatch.setattr(_post_edit, "_new_since_base", lambda st, full, cwd, key: key)
    st = SimpleNamespace(native_on=True, convo=[{"role": "assistant", "content": "ACTION: file_write"}],
                         step_n=1)
    list(_post_edit.post_edit_diagnostics(st, [("a.py", str(f))], str(tmp_path)))
    assert len(st.convo) == 1 and st.held_notes and "Undefined name" in st.held_notes[0]


# ── trace: which added lines ran ──────────────────────────────────────────

_BASE = textwrap.dedent('''\
    def parse(tokens):
        head = tokens.pop(0)
        if head == "CREATE":
            return parse_create(tokens)
        return ("command", tokens)
    ''')
_BUGGY = _BASE + textwrap.dedent('''\


    def parse_create(tokens):
        kind = tokens.pop(0)          # consumes TRIGGER
        if not tokens or tokens[0] != "TRIGGER":
            return ("command", tokens)
        name = tokens[1]
        timing = tokens[2]
        return ("trigger", name, timing)
    ''')
_TEST = textwrap.dedent('''\
    from pkg.mod import parse

    def test_trigger():
        assert parse(["CREATE", "TRIGGER", "t", "AFTER"])[0] == "trigger"
    ''')


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text(_BASE)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text(_TEST)
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                              "commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _red():
    return {"ok": False, "stdout": "F\n=== short test summary info ===\n"
            "FAILED tests/test_mod.py::test_trigger - AssertionError\n1 failed in 0.1s\n"}


def test_the_same_failure_after_a_fix_gets_a_trace_that_points_at_the_early_return(tmp_path):
    head = _repo(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text(_BUGGY)
    st = SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=1, readonly_mode=False,
                         plan_mode=False)
    cmd = {"cmd": f"{sys.executable} -m pytest tests/test_mod.py -q"}
    assert _trace_hint.after_test(st, "run_command", cmd, _red(), str(tmp_path)) == ""   # first red
    st.edits_made = 2                                  # a fix that did not help
    note = _trace_hint.after_test(st, "run_command", cmd, _red(), str(tmp_path))
    assert "line trace" in note
    assert "if not tokens or tokens[0] != \"TRIGGER\"" in note or "return (\"command\", tokens)" in note
    assert "never ran" in note


def test_no_trace_without_a_fix_between_or_when_green_or_off(tmp_path, monkeypatch):
    head = _repo(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text(_BUGGY)
    st = SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=1, readonly_mode=False,
                         plan_mode=False)
    cmd = {"cmd": f"{sys.executable} -m pytest tests/test_mod.py -q"}
    _trace_hint.after_test(st, "run_command", cmd, _red(), str(tmp_path))
    assert _trace_hint.after_test(st, "run_command", cmd, _red(), str(tmp_path)) == ""   # no edit
    assert _trace_hint.after_test(st, "run_command", cmd, {"ok": True, "stdout": "1 passed"},
                                  str(tmp_path)) == ""
    monkeypatch.setenv("AIFORGE_CHAT_TRACE_HINT", "0")
    st.edits_made = 5
    assert _trace_hint.after_test(st, "run_command", cmd, _red(), str(tmp_path)) == ""



def test_slow_parallel_or_background_runs_are_not_traced(tmp_path):
    head = _repo(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text(_BUGGY)
    slow = {"ok": False, "stdout": "F\n=== short test summary info ===\n"
            "FAILED tests/test_mod.py::test_trigger - AssertionError\n1 failed in 95.20s\n"}
    for args, res in (({"cmd": f"{sys.executable} -m pytest tests -q"}, slow),
                      ({"cmd": f"{sys.executable} -m pytest -n 4 tests -q"}, _red()),
                      ({"cmd": f"{sys.executable} -m pytest tests -q", "background": True}, _red())):
        st = SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=1, readonly_mode=False,
                             plan_mode=False)
        _trace_hint.after_test(st, "run_command", args, res, str(tmp_path))
        st.edits_made = 2
        assert _trace_hint.after_test(st, "run_command", args, res, str(tmp_path)) == ""


def test_a_trace_attempt_counts_even_when_it_finds_nothing(tmp_path, monkeypatch):
    head = _repo(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text(_BUGGY)
    monkeypatch.setattr(_trace_hint, "trace", lambda *a, **k: {})     # a timeout
    st = SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=1, readonly_mode=False,
                         plan_mode=False)
    cmd = {"cmd": f"{sys.executable} -m pytest tests/test_mod.py -q"}
    _trace_hint.after_test(st, "run_command", cmd, _red(), str(tmp_path))
    for i in range(6):
        st.edits_made += 1
        _trace_hint.after_test(st, "run_command", cmd, _red(), str(tmp_path))
    assert st.trace_shown == _trace_hint._MAX_PER_TURN


def test_the_formatter_syntax_and_structure_notes_are_held():
    import inspect
    from aiforge_core.runtime.chat_agent._turn import _action, _post_edit, _structure_guard
    assert "hold(st, f\"[automated formatter" in inspect.getsource(_post_edit)
    assert "hold(st, \"[automated syntax check" in inspect.getsource(_action)
    assert "hold(st, \"[automated structure check" in inspect.getsource(_structure_guard)


def test_no_trace_in_the_sandbox_or_builder_or_without_a_real_interpreter(tmp_path, monkeypatch):
    from aiforge_core.runtime import docker_sandbox
    head = _repo(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text(_BUGGY)
    cmd = {"cmd": f"{sys.executable} -m pytest tests/test_mod.py -q"}

    def twice(st, args):
        _trace_hint.after_test(st, "run_command", args, _red(), str(tmp_path))
        st.edits_made += 1
        return _trace_hint.after_test(st, "run_command", args, _red(), str(tmp_path))
    mk = lambda **k: SimpleNamespace(head0=head, cwd=str(tmp_path), edits_made=1,
                                     readonly_mode=False, plan_mode=False, **k)
    assert twice(mk(builder="web"), cmd) == ""
    assert twice(mk(), {"cmd": "source .venv/bin/activate && pytest tests -q"}) == ""
    monkeypatch.setattr(docker_sandbox, "is_enabled", lambda: True)
    assert twice(mk(), cmd) == ""


def test_docstrings_are_not_reported_as_never_run():
    import tempfile, os
    src = 'def f():\n    """Doc\n    more doc\n    """\n    return 1\n'
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
    try:
        assert _trace_hint._stmt_lines(fh.name) == {5}
        note = _trace_hint.report(os.path.dirname(fh.name), {os.path.realpath(fh.name): [(1, 5)]},
                                  {os.path.realpath(fh.name): [5]})
        assert note == ""                      # everything that can run ran
    finally:
        os.unlink(fh.name)


def test_the_hook_note_is_held():
    import inspect
    from aiforge_core.runtime.chat_agent._turn import _action
    src = inspect.getsource(_action._post_tool)
    assert 'hold(st, f"[hook PostToolUse' in src and '_hooks.note_into(None' in src
