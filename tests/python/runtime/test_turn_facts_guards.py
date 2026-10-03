"""The FINAL of a turn that changed SOME files, checked against the disk and
against the turn's own commands (``_guards/turn_facts``).

The live case these pin ("its ok continue simplfying all files"): two of four
files rewritten, the agent's own check failed twice after the last write, and
the answer said "All files simplified … verified all green" with a
before/after row for files no write had touched.
"""
from __future__ import annotations

import subprocess
import types

import pytest

from aiforge_core.runtime import action_log
from aiforge_core.runtime.chat_agent._guards import turn_facts as tf
from aiforge_core.runtime.chat_agent._turn import _finish

_ROW_DB = ("| `database.py` | 96 lines, 10-branch if-chain, unused `sqlite3` "
           "import | 15 lines — `run` maps kind to a SQL statement; unused "
           "`sqlite3` import removed |")
_ANSWER = ("All files simplified, behaviour identical.\n\n"
           "| File | Before | After |\n|---|---|---|\n"
           "| `pipeline.py` | 18 lines | 14 lines — rewritten around one set |\n"
           + _ROW_DB + "\n\nVerified all green.")


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    for name in ("pipeline.py", "database.py", "report.py", "app.py"):
        (tmp_path / name).write_text(f"# {name}\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "deep.py").write_text("x = 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()
    return tmp_path, head


def _drive(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def _st(**kw):
    base = dict(convo=[], edits_made=1, head0=None, session_id=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


# ── what the turn changed ───────────────────────────────────────────────────

def test_changed_files_sees_commits_dirty_and_new_files(repo):
    cwd, head = repo
    assert tf.changed_files(cwd, head) == set()
    (cwd / "pipeline.py").write_text("committed\n")
    _git(cwd, "commit", "-q", "-am", "work")
    (cwd / "app.py").write_text("dirty\n")
    (cwd / "new.py").write_text("new\n")
    assert tf.changed_files(cwd, head) == {"pipeline.py", "app.py", "new.py"}


def test_changed_files_has_no_verdict_without_git(tmp_path):
    assert tf.changed_files(tmp_path, "abc123") is None
    assert tf.changed_files(tmp_path, None) is None
    assert tf.changed_files("", "abc123") is None


# ── what the answer says it changed ─────────────────────────────────────────

def test_a_table_row_with_a_change_verb_names_its_file(repo):
    cwd, _ = repo
    assert tf.claimed_changed(_ANSWER, cwd) == ["pipeline.py", "database.py"]


@pytest.mark.parametrize("text", [
    "`database.py` was not modified.",
    "Tests untouched: `report.py` still matches the first commit.",
    "`database.py` should be simplified next.",
    "In the previous turn I rewrote `pipeline.py`.",
    "I previously simplified `pipeline.py`.",
    "I rewrote `missing_file.py`.",                      # not a file here
    "The entry point is `app.py`; it imports `pipeline.py`.",
    # Live false positive: the verb is about another file, before the ';'.
    "- **notes_tmp.txt** — removed; directory now contains just `app.py` and `report.py`.",
    "Fixed the import bug; `report.py` untouched.",
])
def test_a_mention_that_is_not_a_change_claim_is_left_alone(repo, text):
    assert tf.claimed_changed(text, repo[0]) == []


def test_a_bullet_or_sentence_names_the_files_next_to_its_verb(repo):
    cwd = repo[0]
    assert tf.claimed_changed("- **database.py** — rewritten as a table", cwd) \
        == ["database.py"]
    assert tf.claimed_changed("I simplified `database.py` and `report.py`; "
                              "`app.py` is as it was.", cwd) \
        == ["database.py", "report.py"]


def test_a_bare_basename_resolves_to_the_one_tracked_file(repo):
    assert tf.claimed_changed("Rewrote `deep.py`.", repo[0]) == ["pkg/deep.py"]


# ── the file-claim guard ────────────────────────────────────────────────────

def _file_guard(cwd):
    return tf.UnchangedFileClaimGuard(str(cwd), False, "", False)


def test_a_claim_on_an_untouched_file_sends_the_model_the_facts_once(repo):
    cwd, head = repo
    (cwd / "pipeline.py").write_text("rewritten\n")
    st, step = _st(head0=head), {"text": _ANSWER}
    events, sig = _drive(_file_guard(cwd).check(st, step))
    assert sig == "continue"
    assert events == [{"type": "thought", "role": "system",
                       "text": tf.UnchangedFileClaimGuard.notice}]
    note = st.convo[-1]["content"]
    assert note.startswith("[harness — not the user]")
    assert "`database.py`" in note and "did change: `pipeline.py`" in note
    # SAME: the answer stands, with the facts from the disk under it.
    step = {"text": "SAME"}
    events, sig = _drive(_file_guard(cwd).check(st, step))
    assert sig is None and events == []
    assert step["text"] == (_ANSWER + "\n\n(Changed on disk in this turn: "
                            "`pipeline.py`. Not changed: `database.py`.)")
    # Asked once: a later final is not checked again.
    step = {"text": _ANSWER}
    assert _drive(_file_guard(cwd).check(st, step)) == ([], None)
    assert step["text"] == _ANSWER


def test_the_work_done_after_the_note_ends_the_check_without_a_footer(repo):
    cwd, head = repo
    (cwd / "pipeline.py").write_text("rewritten\n")
    st = _st(head0=head)
    assert _drive(_file_guard(cwd).check(st, {"text": _ANSWER}))[1] == "continue"
    (cwd / "database.py").write_text("rewritten too\n")
    step = {"text": _ANSWER}
    assert _drive(_file_guard(cwd).check(st, step)) == ([], None)
    assert step["text"] == _ANSWER


@pytest.mark.parametrize("kw", [
    dict(head0=None),                       # no git baseline
])
def test_the_file_guard_is_silent_without_a_baseline(repo, kw):
    cwd, _ = repo
    (cwd / "pipeline.py").write_text("rewritten\n")
    step = {"text": _ANSWER}
    assert _drive(_file_guard(cwd).check(_st(**kw), step)) == ([], None)


def test_the_file_guard_leaves_a_turn_that_changed_nothing_to_the_other_guards(repo):
    cwd, head = repo
    step = {"text": _ANSWER}
    assert _drive(_file_guard(cwd).check(_st(head0=head), step)) == ([], None)


def test_plan_builder_and_the_switch_turn_the_file_guard_off(repo, monkeypatch):
    cwd, head = repo
    (cwd / "pipeline.py").write_text("rewritten\n")
    for guard in (tf.UnchangedFileClaimGuard(str(cwd), True, "", False),
                  tf.UnchangedFileClaimGuard(str(cwd), False, "skill", False),
                  tf.UnchangedFileClaimGuard(str(cwd), False, "", True)):
        assert _drive(guard.check(_st(head0=head), {"text": _ANSWER})) == ([], None)
    monkeypatch.setenv("AIFORGE_CHAT_FILE_CLAIM_GUARD", "0")
    assert _drive(_file_guard(cwd).check(_st(head0=head), {"text": _ANSWER})) == ([], None)


# ── a command that failed after the last change ─────────────────────────────

def _cmd(cmd, code, out=""):
    return {"type": "tool", "name": "run_command", "args": {"cmd": cmd},
            "result": {"ok": code == 0, "code": code, "stdout": "", "stderr": out}}


def _write(path):
    return {"type": "tool", "name": "file_write", "args": {"path": path},
            "result": {"ok": True, "path": path}}


_ASSERT = "Traceback (most recent call last):\nAssertionError: ('k0', {'n': 6})"
_LIVE = [
    _write("app.py"), _cmd('python3 -c "check 1"', 1, _ASSERT),
    _cmd('python3 -c "check 2"', 0), _write("pipeline.py"), _write("app.py"),
    _cmd('python3 -c "check 3"', 1, _ASSERT), _cmd('python3 -c "check 4"', 1, _ASSERT),
    {"type": "tool", "name": "run_command", "args": {"cmd": "git add -A"},
     "result": {"ok": False, "blocked": "blanket_git", "error": "disabled"}},
    _cmd("git add app.py && git commit -m x", 0),
]


def test_the_live_sequence_reports_the_checks_that_failed_after_the_last_write():
    assert tf.unpassed_failures(_LIVE) == [
        ('python3 -c "check 3"', "exit 1: AssertionError: ('k0', {'n': 6})"),
        ('python3 -c "check 4"', "exit 1: AssertionError: ('k0', {'n': 6})")]


@pytest.mark.parametrize("steps", [
    [_cmd("python3 -m pytest -q", 1, "FAILED test_a.py"), _write("a.py"),
     _cmd("python3 -m pytest -q", 0)],                      # failed, fixed, passed
    [_write("a.py"), _cmd("python3 -m pytest -q", 1, "FAILED test_a.py"),
     _cmd("cd . && python3 -m pytest -q -x", 0)],           # same program passed later
    [_write("a.py"), _cmd("grep -n foo a.py", 1)],           # no match is not an error
    [_cmd("python3 -m pytest -q", 1, "FAILED test_a.py")],   # nothing was written
    [],
])
def test_no_finding_when_the_failure_is_not_the_last_word(steps):
    assert tf.unpassed_failures(steps) == []


def test_the_program_of_a_shell_line():
    assert tf._program("cd app && FOO=1 python3 -m pytest -q") == "python3"
    assert tf._program("env FOO=1 ./gradlew test; echo done") == "gradlew"
    assert tf._program("") == ""


@pytest.fixture
def live_run():
    run = action_log.begin_run(987654)
    yield run
    action_log.end_run(run)


def _check_guard():
    return tf.UnpassedCheckGuard(False, "", False)


def test_a_failed_check_is_put_to_the_model_once_and_stays_under_a_same(live_run):
    for ev in _LIVE:
        action_log.observe(live_run, ev)
    st, step = _st(session_id=987654), {"text": "Verified all green."}
    events, sig = _drive(_check_guard().check(st, step))
    assert sig == "continue"
    assert events == [{"type": "thought", "role": "system",
                       "text": tf.UnpassedCheckGuard.notice}]
    note = st.convo[-1]["content"]
    assert note.startswith("[harness — not the user]")
    assert '- `python3 -c "check 4"` — exit 1: AssertionError' in note
    step = {"text": "SAME"}
    assert _drive(_check_guard().check(st, step)) == ([], None)
    assert step["text"] == (
        "Verified all green.\n\n(After the last file change, "
        "`python3 -c \"check 4\"` ended with an error (exit 1: AssertionError: "
        "('k0', {'n': 6})) and was not run again successfully.)")


def test_a_rerun_that_passes_after_the_note_leaves_the_answer_alone(live_run):
    for ev in _LIVE:
        action_log.observe(live_run, ev)
    st = _st(session_id=987654)
    assert _drive(_check_guard().check(st, {"text": "Verified."}))[1] == "continue"
    action_log.observe(live_run, _write("pipeline.py"))
    action_log.observe(live_run, _cmd('python3 -c "check 5"', 0))
    step = {"text": "Fixed the offset; the check passes now."}
    assert _drive(_check_guard().check(st, step)) == ([], None)
    assert step["text"] == "Fixed the offset; the check passes now."


def test_the_check_guard_needs_a_session_and_an_edit(live_run, monkeypatch):
    for ev in _LIVE:
        action_log.observe(live_run, ev)
    for st in (_st(session_id=None), _st(session_id=987654, edits_made=0)):
        assert _drive(_check_guard().check(st, {"text": "Done."})) == ([], None)
    plan = tf.UnpassedCheckGuard(False, "", True)
    assert _drive(plan.check(_st(session_id=987654), {"text": "Done."})) == ([], None)
    monkeypatch.setenv("AIFORGE_CHAT_FAILED_CHECK_GUARD", "0")
    assert _drive(_check_guard().check(_st(session_id=987654), {"text": "Done."})) == ([], None)


# ── through the loop's entry point, in order ────────────────────────────────

def test_handle_final_runs_both_checks_and_a_same_restores_each_answer(
        repo, live_run, monkeypatch):
    cwd, head = repo
    monkeypatch.setattr(_finish, "_fire_stop", lambda *a, **k: None)
    monkeypatch.setattr(_finish, "_endpoint_one_slot", lambda: True)
    monkeypatch.setattr(_finish, "_verify_on_final", lambda *a, **k: iter(()))
    (cwd / "pipeline.py").write_text("rewritten\n")
    for ev in _LIVE:
        action_log.observe(live_run, ev)
    st = types.SimpleNamespace(
        convo=[], board_used=False, board={}, edits_made=2, readonly_mode=False,
        continue_nudges=0, action_counts={}, edit_claim_nudges=0, verify_rounds=99,
        goal="continue simplifying all files", no_change_nudges=0,
        head0=head, session_id=987654)

    def final(text):
        step = {"type": "final", "text": text}
        return _drive(_finish._handle_final(st, step, "", False, False, False,
                                            str(cwd), [], "")) + (step,)

    events, sig, _ = final(_ANSWER)
    assert sig == "continue" and "names files as changed" in events[-1]["text"]
    events, sig, _ = final("SAME")
    assert sig == "continue" and "a command failed after" in events[-1]["text"]
    events, sig, _ = final("SAME")
    assert sig == "return"
    message = next(e for e in events if e["type"] == "message")["text"]
    assert message.startswith(_ANSWER)
    assert "(Changed on disk in this turn: `pipeline.py`. Not changed: `database.py`.)" in message
    assert "was not run again successfully.)" in message
