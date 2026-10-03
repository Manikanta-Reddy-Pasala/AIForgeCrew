"""A big request is small tasks done one by one, each in a fresh context.

Chat mode: the task board items are closed only with evidence (files really
changed, or a green test/build), get a result note written from that evidence,
and the next item starts from a context that holds the system prompt, the
pinned task, the board with the notes, and nothing of the finished item's
transcript. Team mode: every subtask is a fresh prompt (spec + board + notes +
its own spec) and counts only when its commit changed something.
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime.chat_agent._turn import _items, _tasks
from aiforge_core.runtime.parallel_subtasks import _items as sub_items


def _scripted(outputs):
    seq = list(outputs)
    calls = []

    def _fn(role, convo):
        calls.append(list(convo))
        return seq.pop(0)
    return _fn, calls


def _run(tmp, fn, prompt="do the big job", **kw):
    return list(ca.run_chat_agent([{"role": "user", "content": prompt}],
                                  cwd=str(tmp), complete_fn=fn, **kw))


def _plan(slug, **kw):
    return f"ACTION: plan_progress\nARGS_JSON: {json.dumps({'slug': slug, **kw})}"


def _write(path, content):
    return ("ACTION: file_write\nARGS_JSON: "
            + json.dumps({"path": path, "content": content}))


def _text(convo) -> str:
    return "\n".join(str(m.get("content")) for m in convo)


def _board3():
    return {"a": {"title": "first", "status": "pending"},
            "b": {"title": "second", "status": "pending"},
            "c": {"title": "third", "status": "pending"}}


def _st(tmp_path, board=None, **kw):
    st = SimpleNamespace(board=board or _board3(), readonly_mode=False,
                         builder=None, cwd=str(tmp_path), file_hashes={},
                         tree_hashes={}, tree_pending=False, goal="the goal",
                         steers=[], unlimited=False, git_off=True, git_root="",
                         pending_steps=[], batch_unread=False,
                         read_sigs_seen=set(), batch_mark=1, early_reads={},
                         convo=[{"role": "system", "content": "SYS"}],
                         **_items.item_fields())
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _close(st, slug, status="done"):
    st.board[slug]["status"] = status
    return _items.review_progress(
        st, {"ok": True, "slug": slug, "status": status}, [])


# ── chat: end to end ─────────────────────────────────────────────────────

def test_three_items_each_start_from_a_fresh_context(tmp_path):
    fn, calls = _scripted([
        _plan("a", title="first thing"),
        _plan("b", title="second thing"),
        _plan("c", title="third thing"),
        _write("a.txt", "SECRET_ITEM1_TRANSCRIPT"),
        _plan("a", status="done"),
        _write("b.txt", "SECRET_ITEM2_TRANSCRIPT"),
        _plan("b", status="done"),
        _write("c.txt", "item three"),
        _plan("c", status="done"),
        "FINAL: all three done",
    ])
    evs = _run(tmp_path, fn)
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "all three done"
    assert [e for e in evs if e["type"] == "thought"
            and "cleared the working context" in e.get("text", "")]
    # call 6 is item 2's first step: the context was reset after item 1.
    item2 = calls[5]
    assert [m["role"] for m in item2] == ["system", "user"]
    assert "SECRET_ITEM1_TRANSCRIPT" not in _text(item2)
    assert "do the big job" in item2[0]["content"]            # the pinned task
    assert _tasks._BOARD_OPEN in item2[0]["content"]          # the board
    assert "a.txt" in item2[0]["content"]                     # item 1's result note
    assert "first thing" in item2[0]["content"] and "second thing" in item2[0]["content"]
    assert "Do next: b: second thing" in item2[1]["content"]
    assert "memory_lookup" in item2[0]["content"]             # the saved transcript
    # call 8 is item 3: item 1 AND item 2 are notes now, not transcript.
    item3 = calls[7]
    assert "SECRET_ITEM1_TRANSCRIPT" not in _text(item3)
    assert "SECRET_ITEM2_TRANSCRIPT" not in _text(item3)
    assert "a.txt" in item3[0]["content"] and "b.txt" in item3[0]["content"]
    # the work itself landed
    assert (tmp_path / "c.txt").read_text() == "item three"


def test_an_item_with_no_evidence_is_sent_back_then_accepted_unverified(tmp_path):
    fn, calls = _scripted([
        _plan("a", title="first"), _plan("b", title="second"),
        _plan("c", title="third"),
        _plan("a", status="done", note="1"),  # nothing happened: rejected
        _plan("a", status="done", note="2"),  # rejected again (different hint)
        _plan("a", status="done", note="3"),  # out of retries: unverified
        _plan("b", status="skipped"),
        _plan("c", status="skipped"),
        "FINAL: stop"])
    _run(tmp_path, fn)
    first = str(calls[4][-1]["content"])
    assert "not accepted as done" in first and "nothing shows" in first
    second = str(calls[5][-1]["content"])
    assert "DIFFERENT approach" in second
    assert "UNVERIFIED" in calls[6][0]["content"]       # note on the board, after the reset


def test_a_small_run_is_untouched(tmp_path):
    """Two items: no evidence gate, no reset, the transcript stays."""
    fn, calls = _scripted([
        _plan("a", title="first"), _plan("b", title="second"),
        _write("a.txt", "TWO_ITEM_MARKER"),
        _plan("a", status="done"),
        _plan("b", status="done"),
        "FINAL: ok"])
    evs = _run(tmp_path, fn)
    assert not [e for e in evs if "cleared the working context" in str(e.get("text"))]
    assert "TWO_ITEM_MARKER" in _text(calls[4])         # still in the window
    assert len(calls) == 6                                # no extra model call


def test_reset_off_switch_keeps_the_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_ITEM_CONTEXT_RESET", "0")
    fn, calls = _scripted([
        _plan("a", title="first"), _plan("b", title="second"),
        _plan("c", title="third"),
        _write("a.txt", "KEEP_ME_MARKER"), _plan("a", status="done"),
        _write("b.txt", "x"), _plan("b", status="done"),
        _write("c.txt", "y"), _plan("c", status="done"), "FINAL: ok"])
    _run(tmp_path, fn)
    assert "KEEP_ME_MARKER" in _text(calls[5])


# ── chat: the evidence rule ──────────────────────────────────────────────

def test_a_changed_file_is_evidence_and_the_note_names_it(tmp_path):
    st = _st(tmp_path)
    _items.ensure_started(st)
    (tmp_path / "x.py").write_text("v1")
    st.file_hashes[str(tmp_path / "x.py")] = "h1"
    _items.note_evidence(st, "file_write", {}, {"ok": True})
    result, _ = _close(st, "a")
    assert result["ok"] and st.board["a"]["status"] == "done"
    assert "x.py" in st.board["a"]["note"] and st.item_reset_pending


def test_a_failing_test_run_blocks_done_then_marks_failed(tmp_path):
    st = _st(tmp_path)
    _items.ensure_started(st)
    st.file_hashes[str(tmp_path / "x.py")] = "h1"
    red = {"ok": False, "stdout": "1 failed, 2 passed in 0.1s", "exit_code": 1}
    _items.note_evidence(st, "run_tests", {}, red)
    for tries in range(_items.max_retries()):
        result, events = _close(st, "a")
        assert result["ok"] is False and st.board["a"]["status"] == "running"
        assert "FAILED" in result["error"] and events[0]["status"] == "running"
        assert not st.item_reset_pending
    result, _ = _close(st, "a")                  # out of retries
    assert st.board["a"]["status"] == "failed"
    assert "verification failed" in st.board["a"]["note"]
    assert "1 failed" in st.board["a"]["note"]


def test_a_green_test_run_is_evidence_even_without_edits(tmp_path):
    st = _st(tmp_path)
    _items.ensure_started(st)
    green = {"ok": True, "stdout": "===== 12 passed in 0.5s =====", "exit_code": 0}
    _items.note_evidence(st, "run_tests", {}, green)
    result, _ = _close(st, "a")
    assert result["ok"] and "tests green" in st.board["a"]["note"]
    assert "12 passed" in st.board["a"]["note"]


def test_a_command_that_succeeded_is_evidence_and_the_note_names_it(tmp_path):
    st = _st(tmp_path)
    _items.ensure_started(st)
    _items.note_evidence(st, "run_command", {"cmd": "curl -sf localhost:8000/health"},
                         {"ok": True, "code": 0, "stdout": "200"})
    result, _ = _close(st, "a")
    assert result["ok"] and st.board["a"]["status"] == "done"
    assert "ran ok: run_command(curl -sf localhost:8000/health)" in st.board["a"]["note"]
    assert "UNVERIFIED" not in st.board["a"]["note"]


def test_an_external_tool_call_that_returned_ok_is_evidence(tmp_path):
    st = _st(tmp_path)
    _items.ensure_started(st)
    _items.note_evidence(st, "jira_comment", {"key": "ONE-1"}, {"ok": True, "id": 7})
    result, _ = _close(st, "a")
    assert result["ok"] and "ran ok: jira_comment(ONE-1)" in st.board["a"]["note"]


def test_a_failed_command_is_not_evidence(tmp_path):
    st = _st(tmp_path)
    _items.ensure_started(st)
    _items.note_evidence(st, "run_command", {"cmd": "git push origin feat"},
                         {"ok": False, "code": 1, "stderr": "fatal: rejected"})
    _items.note_evidence(st, "jira_comment", {"key": "ONE-1"},
                         {"ok": False, "error": "401"})
    result, _ = _close(st, "a")
    assert result["ok"] is False and "nothing shows" in result["error"]
    assert st.board["a"]["status"] == "running"


@pytest.mark.parametrize("name,args", [
    ("file_read", {"path": "a.py"}),
    ("grep", {"pattern": "x"}),
    ("editor", {"command": "view", "path": "a.py"}),
    ("run_command", {"cmd": "cat a.py"}),
    ("run_command", {"cmd": "git status"}),
])
def test_reads_alone_are_not_evidence(tmp_path, name, args):
    st = _st(tmp_path)
    _items.ensure_started(st)
    _items.note_evidence(st, name, args, {"ok": True, "code": 0, "stdout": "x"})
    result, _ = _close(st, "a")
    assert result["ok"] is False and "nothing shows" in result["error"]


def test_a_running_command_and_a_write_that_changed_nothing_are_not_evidence(
        tmp_path):
    st = _st(tmp_path)
    _items.ensure_started(st)
    _items.note_evidence(st, "run_command", {"cmd": "npm run dev"},
                         {"ok": True, "running": True, "id": "bg-1"})
    _items.note_evidence(st, "file_write", {"path": "a.py"}, {"ok": True})
    result, _ = _close(st, "a")
    assert result["ok"] is False and "nothing shows" in result["error"]


def test_a_command_before_the_item_started_is_not_its_evidence(tmp_path):
    st = _st(tmp_path)
    _items.ensure_started(st)
    _items.note_evidence(st, "run_command", {"cmd": "make deploy"},
                         {"ok": True, "code": 0})
    assert _close(st, "a")[0]["ok"]
    st.item_fresh_close = False                  # a new model step began
    _items.note_evidence(st, "file_read", {"path": "a.py"}, {"ok": True})
    result, _ = _close(st, "b")
    assert result["ok"] is False and "nothing shows" in result["error"]


def test_several_items_closed_in_one_reply_share_the_evidence(tmp_path):
    st = _st(tmp_path)
    _items.ensure_started(st)
    st.file_hashes[str(tmp_path / "x.py")] = "h1"
    _items.note_evidence(st, "file_write", {}, {"ok": True})
    assert _close(st, "a")[0]["ok"]
    assert _close(st, "b")[0]["ok"]               # same reply: no tool call between
    st.item_fresh_close = False                   # a model step went by
    assert _close(st, "c")[0]["ok"] is False      # c itself did nothing


def test_verify_off_switch_accepts_without_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_ITEM_VERIFY", "0")
    st = _st(tmp_path)
    _items.ensure_started(st)
    result, _ = _close(st, "a")
    assert result["ok"] and st.board["a"]["status"] == "done"


def test_a_two_item_board_is_not_gated(tmp_path):
    board = {"a": {"title": "x", "status": "pending"},
             "b": {"title": "y", "status": "pending"}}
    st = _st(tmp_path, board)
    assert not _items.active(st)
    result, _ = _close(st, "a")
    assert result == {"ok": True, "slug": "a", "status": "done"}
    assert not st.item_reset_pending


def test_the_note_is_built_from_evidence_not_from_the_model(tmp_path):
    st = _st(tmp_path)
    _items.ensure_started(st)
    st.file_hashes[str(tmp_path / "real.py")] = "h"
    _items.note_evidence(st, "file_write", {}, {"ok": True})
    st.board["a"]["status"] = "done"
    result, _ = _items.review_progress(
        st, {"ok": True, "slug": "a", "status": "done",
             "note": "I fixed everything perfectly"}, [])
    assert "perfectly" not in st.board["a"]["note"]
    assert "real.py" in st.board["a"]["note"]


# ── chat: the reset itself ───────────────────────────────────────────────

def test_reset_context_keeps_system_goal_board_and_drops_the_rest(tmp_path):
    st = _st(tmp_path)
    st.convo = [{"role": "system", "content": "SYS"},
                {"role": "user", "content": "do the big job"},
                {"role": "assistant", "content": "ACTION: x RAW_FIRST_ITEM"},
                {"role": "user", "content": "OBSERVATION: ok"}]
    st.board["a"].update(status="done", note="done: files: a.py")
    st.item_last_closed = "a"
    assert _items.reset_context(st)
    assert [m["role"] for m in st.convo] == ["system", "user"]
    sys_text = st.convo[0]["content"]
    assert sys_text.startswith("SYS") and "RAW_FIRST_ITEM" not in _text(st.convo)
    assert "ORIGINAL TASK" in sys_text and "the goal" in sys_text
    assert "result: done: files: a.py" in sys_text
    assert "Do next: b: second" in st.convo[1]["content"]


def test_reset_waits_for_queued_calls_and_unread_results(tmp_path):
    st = _st(tmp_path)
    st.item_reset_pending = True
    st.pending_steps = ["ACTION: file_read"]
    assert not _items.reset_pending_ready(st)
    st.pending_steps = []
    st.batch_unread = True
    assert not _items.reset_pending_ready(st)
    st.batch_unread = False
    st.board["a"]["status"] = "done"
    assert _items.reset_pending_ready(st)


def test_no_reset_after_the_last_item(tmp_path):
    st = _st(tmp_path)
    for s in ("b", "c"):
        st.board[s]["status"] = "done"
    _items.ensure_started(st)
    st.file_hashes[str(tmp_path / "z.py")] = "h"
    _items.note_evidence(st, "file_write", {}, {"ok": True})
    _close(st, "a")
    assert not st.item_reset_pending


# ── decomposition wording ────────────────────────────────────────────────

def test_the_rules_make_decomposition_mandatory_for_big_requests(tmp_path):
    from aiforge_core.runtime.chat_agent._prompt_text import (
        DECOMPOSE_RULE,
        LONG_RUN_RULE,
    )
    from aiforge_core.runtime.chat_agent._turn._convo import _build_convo
    for rule in (LONG_RUN_RULE, DECOMPOSE_RULE):
        assert "ONE BY ONE" in rule and "plan_progress" in rule
        assert "3 or more steps" in rule
    kw = dict(readonly_mode=False, plan_mode=False, analyze_mode=False,
              builder=None, strict_finish=False, session_id=None)
    big = [{"role": "user", "content": "fix the login bug. also add a retry to "
            "the sync client. and update the README"}]
    one_line = [{"role": "user", "content": "rename foo to bar"}]
    capped = _build_convo(big, str(tmp_path), "chat", **kw)[0][0]["content"]
    small = _build_convo(one_line, str(tmp_path), "chat", **kw)[0][0]["content"]
    assert "BIG TASK = SMALL TASKS" in capped
    assert "BIG TASK = SMALL TASKS" not in small


# ── team mode ────────────────────────────────────────────────────────────

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README").write_text("r")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def _subs():
    return [{"slug": "s1", "goal": "write a", "path": "a.py"},
            {"slug": "s2", "goal": "write b", "path": "b.py"},
            {"slug": "s3", "goal": "write c", "path": "c.py"}]


def test_each_subtask_prompt_has_the_board_and_notes_but_not_the_last_transcript(repo):
    from aiforge_core.runtime.parallel_subtasks import _orchestrate, _runners
    seen: list = []

    def run_one(sub, wt):
        seen.append(dict(sub))
        (repo / sub["path"]).write_text(f"# {sub['slug']}\nTRANSCRIPT_{sub['slug']}\n")
        return {"ok": True, "files": [sub["path"]]}

    agg = _orchestrate.run_parallel(str(repo), "HEAD", None, _subs(), run_one,
                                    in_place=True)
    assert agg["ok"] and agg["done"] == 3
    first, second, third = seen
    assert ">>> s1" in first["_board"] and "result:" not in first["_board"]
    assert "[x] s1: write a" in second["_board"] and ">>> s2" in second["_board"]
    assert "result: files: a.py (+2 -0)" in second["_board"]
    assert "result: files: b.py (+2 -0)" in third["_board"]
    msg = _runners._doer_message(second, "SPEC TEXT", "b.py", "write b")
    assert "TASK BOARD" in msg and "SPEC TEXT" in msg and "write b" in msg
    assert "TRANSCRIPT_s1" not in msg
    lite = _runners._subtask_prompt(second, "SPEC TEXT", "b.py", "write b")
    assert "TASK BOARD" in lite and "[x] s1" in lite


def test_a_subtask_that_changed_nothing_is_not_done(repo):
    from aiforge_core.runtime.parallel_subtasks import _orchestrate
    calls = []

    def lazy(sub, wt):
        calls.append(sub["slug"])
        return {"ok": True}

    agg = _orchestrate.run_parallel(str(repo), "HEAD", None, _subs()[:1], lazy,
                                    in_place=True)
    assert agg["ok"] is False and agg["failed"] == 1
    assert "changed no file" in str(agg["results"][0]["validation"]) \
        or "changed no file" in str(agg["results"][0]["error"])


def test_subtask_evidence_off_switch(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_SUBTASK_EVIDENCE", "0")
    from aiforge_core.runtime.parallel_subtasks import _orchestrate
    agg = _orchestrate.run_parallel(str(repo), "HEAD", None, _subs()[:1],
                                    lambda s, w: {"ok": True}, in_place=True)
    assert agg["ok"] is True


def test_board_switch_off_leaves_the_prompt_alone(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_SUBTASK_BOARD", "0")
    from aiforge_core.runtime.parallel_subtasks import _orchestrate
    seen = []

    def run_one(sub, wt):
        seen.append(dict(sub))
        (repo / sub["path"]).write_text("x")
        return {"ok": True}
    _orchestrate.run_parallel(str(repo), "HEAD", None, _subs()[:2], run_one,
                              in_place=True)
    assert all("_board" not in s for s in seen)


def test_shape_adds_an_acceptance_check_and_flags_big_subtasks():
    subs = [{"slug": "a", "goal": "g", "path": "src/a.py"},
            {"slug": "t", "goal": "g", "path": "tests/test_a.py"},
            {"slug": "n", "goal": "x" * 900},
            {"slug": "k", "goal": "g", "path": "b.py", "acceptance": ["mine"]}]
    warnings = sub_items.shape_subtasks(subs)
    assert "syntax check" in subs[0]["acceptance"][0]
    assert "runnable tests" in subs[1]["acceptance"][0]
    assert subs[3]["acceptance"] == ["mine"]
    assert any(w.startswith("n:") and "no file and no acceptance" in w for w in warnings)
    assert any(w.startswith("n:") and "900 characters" in w for w in warnings)


def test_the_doer_seed_asks_for_decomposition():
    from aiforge_core.runtime.text_doer_seed import _SEED_HEADER
    assert "SMALL TASKS, ONE BY ONE" in _SEED_HEADER
