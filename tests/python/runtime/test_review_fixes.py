"""Defects a code review found in the merged work (each would bite in a real run)."""
import time
from types import SimpleNamespace

import pytest

from aiforge_core.llm import reasoning
from aiforge_core.runtime import context_offload
from aiforge_core.runtime.chat_agent import _native
from aiforge_core.runtime.chat_agent._context import _aging as A
from aiforge_core.runtime.chat_agent._context import _compaction as C2
from aiforge_core.runtime.chat_agent._context import _note
from aiforge_core.runtime.chat_agent._turn import _completion as C
from aiforge_core.runtime.chat_agent._turn import _items
from aiforge_core.runtime.chat_agent._turn import _progress as P


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_AGE_BURST", "1")


# 1. ping-pong must not lock out every other call ---------------------------

def _pingpong_state():
    st = SimpleNamespace()
    for i in range(6):
        P.note_identical(st, "run_command|a" if i % 2 == 0 else "grep|b",
                         {"ok": True, "stdout": "same"})
    return st


def test_ping_pong_only_holds_the_two_calls_in_the_pattern():
    st = _pingpong_state()
    assert P.ping_pong(st, "run_command|a") and P.ping_pong(st, "grep|b")
    assert not P.ping_pong(st, "file_write|c")       # a different call must run


def test_a_recovery_clears_the_window_so_it_cannot_re_trip_forever():
    st = _pingpong_state()
    st.strikes, st.backstop, st.lifetime = {}, {}, {}
    st.lifetime_forgiven, st.state_fp = {}, ""
    P.forgive(st, "run_command|a")
    assert not P.ping_pong(st, "run_command|a")


# 2/3. aging must not strip unread batch results, and must not cause a refusal --

def _pair(tool, body, args='{"path": "a.py"}'):
    return [{"role": "assistant", "content": f"ACTION: {tool}\nARGS_JSON: {args}"},
            {"role": "user", "content": "OBSERVATION: " + body}]


def test_an_unread_batch_is_never_aged(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_AGE_KEEP", "2")        # the window alone would not protect it
    convo = [{"role": "system", "content": "s"}]
    for i in range(6):
        convo += _pair("file_read", "x" * 6000, f'{{"path": "f{i}.py"}}')
    mark = 1 + 2 * 2                                   # the last 4 reads are unread
    assert A.age_observations(convo, protect_from=mark) == 2     # only the two read ones
    assert all("[aged:" not in m["content"] for m in convo[mark:] if m["role"] == "user")


def test_an_aged_read_may_be_read_again():
    convo = [{"role": "system", "content": "s"}] + _pair("file_read", "x" * 6000)
    for _ in range(6):
        convo += _pair("run_command", "ok", "{}")
    seen = {'file_read|{"path": "a.py"}'}
    assert A.age_observations(convo, forget=seen) == 1
    assert seen == set()          # the duplicate-read guard no longer refuses it


# 4. persist rounds are bounded by failure kind and honour a worker stop -----------

def test_caps_by_failure_kind(monkeypatch):
    from aiforge_core.llm import model_outage as mo
    monkeypatch.setattr(mo, "issue", lambda e: None)
    assert C._persist_caps(mo.OUTAGE, None, mo) == (10 ** 9, 0.0)       # an outage waits
    assert C._persist_caps(mo.CONFIG, None, mo) == (C._CONFIG_ROUNDS, 0.0)
    assert C._persist_caps(mo.SHIPPED, None, mo) == (2, 0.0)            # may still be generating
    monkeypatch.setenv("AIFORGE_CHAT_PERSIST_OTHER_S", "77")
    assert C._persist_caps(mo.OTHER, None, mo) == (10 ** 9, 77.0)       # time-bounded
    monkeypatch.setattr(mo, "issue", lambda e: object())
    assert C._persist_caps(mo.OTHER, None, mo) == (6, 0.0)


def test_a_worker_stop_ends_the_persist_loop(monkeypatch):
    from aiforge_core.llm import model_wait
    from aiforge_core.runtime import run_interrupt
    monkeypatch.setattr(model_wait, "cancel_reason", lambda: "stop")
    monkeypatch.setattr(run_interrupt, "pause", lambda *a, **k: None)
    monkeypatch.setattr(C, "_PERSIST_GAPS", (0.0,))
    gen = C._persist_until_answer(lambda r, c: "x", "doer", [], None,
                                  RuntimeError("HTTP 500"), 0.0)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        assert stop.value[0] is C._CANCELLED


# 5. reasoning boost reserve ---------------------------------------------------

def test_a_boosted_step_reserves_reply_tokens():
    assert reasoning.boost_reserve_tokens() == 0
    with reasoning.boost():
        assert reasoning.boost_reserve_tokens() > 0
    from aiforge_core.runtime.chat_agent._context import _window as W
    assert reasoning.boost_reserve_tokens in W._RESERVE_HOOKS


def test_a_nested_boost_is_not_clobbered_by_a_non_boosted_step():
    with reasoning.boost():
        tok = reasoning._BOOST.set(reasoning.boosted() or False)
        assert reasoning.boosted()
        reasoning._BOOST.reset(tok)


# 6. item reset keeps the system prefix when the note layout is active -----------

def test_item_reset_in_the_note_layout_keeps_the_system_message_byte_identical():
    from aiforge_core.runtime.chat_agent._turn._tasks import seed_board
    board = seed_board(["part one", "part two", "part three"])
    board["part-1"].update(status="done", note="changed a.py (+3/-1)")
    convo = [{"role": "system", "content": "SYSTEM PROMPT"},
             _note.build("goal block", C2._CONDENSE_OPEN + "\nold failed approaches\n" + C2._CONDENSE_CLOSE),
             _note.ack(),
             {"role": "user", "content": "work"},
             {"role": "assistant", "content": "ACTION: file_read\nARGS_JSON: {}"},
             {"role": "user", "content": "OBSERVATION: " + "y" * 200}]
    st = SimpleNamespace(convo=convo, board=board, goal="make it fast", item_resets=0,
                         item_last_closed="part-1", read_sigs_seen={"x"}, batch_mark=0,
                         early_reads={}, pending_steps=[], steers=[], unlimited=False,
                         readonly_mode=False, builder=None, item_reset_pending=True)
    sys_before = convo[0]["content"]
    assert _items.reset_context(st)
    assert st.convo[0]["content"] == sys_before
    assert _note.note_index(st.convo) == 1
    note = st.convo[1]["content"]
    assert "old failed approaches" in note and "changed a.py" in note
    assert "make it fast" in note
    assert [m["role"] for m in st.convo] == ["system", "user", "assistant", "user"]


# 7. a model-outage stop keeps the saved handoff ---------------------------------

def test_an_outage_stop_is_recorded_as_interrupted_not_final():
    from aiforge_core.runtime import handoff_store as H
    rec = H.Recorder.__new__(H.Recorder)
    rec.outcome, rec.steps, rec.st = None, 0, SimpleNamespace(board={}, last_green_fp=None)
    rec._closed, rec._green = 0, None
    rec._closed_count = lambda: 0
    rec.save = lambda *a, **k: None
    rec._observe({"type": "stopped", "reason": "llm_unavailable"})
    rec._observe({"type": "message", "text": "⚠️ The model didn't respond"})
    assert rec.outcome == "interrupted"


# 8. a failed subtask's rollback leaves other files alone -------------------------

def test_rollback_removes_only_the_files_the_subtask_created(tmp_path):
    import subprocess
    from aiforge_core.runtime.parallel_subtasks import _orchestrate as O
    repo = str(tmp_path)
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True)
    run("init", "-q")
    run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("1")
    run("add", "-A"); run("commit", "-q", "-m", "init")
    before = run("rev-parse", "HEAD").stdout.strip()
    (tmp_path / "keep.txt").write_text("someone else's file")   # existed before
    untracked = O._untracked(repo)
    started = time.time()
    time.sleep(0.05)
    (tmp_path / "half-work.py").write_text("x")                   # the failed subtask's
    (tmp_path / "a.txt").write_text("changed")
    O._rollback(repo, before, untracked, started)
    assert (tmp_path / "keep.txt").exists()
    assert not (tmp_path / "half-work.py").exists()
    assert (tmp_path / "a.txt").read_text() == "1"


# 9. offload and read-only commands ---------------------------------------------

def test_never_clear_the_transcript_without_a_saved_copy(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _escalate as E
    monkeypatch.setattr(context_offload, "save", lambda text: None)
    st = SimpleNamespace(convo=[{"role": "system", "content": "s"},
                                {"role": "user", "content": "a"},
                                {"role": "assistant", "content": "b"}],
                         role="doer", goal="g", board={}, file_hashes={})
    assert E.restart_with_handoff(st) is False
    assert len(st.convo) == 3


def test_a_big_transcript_keeps_its_end_and_a_resave_refreshes_the_file():
    big = "HEAD " + "m" * 500_000 + " THE-LATEST-TURN"
    oid = context_offload.save(big)
    assert "THE-LATEST-TURN" in context_offload.load(oid, 10 ** 6)["text"] or \
        "THE-LATEST-TURN" in open(context_offload._path(oid)).read()
    p = context_offload._path(oid)
    import os
    os.utime(p, (1, 1))
    context_offload.save(big)
    assert os.path.getmtime(p) > 1000


@pytest.mark.parametrize("cmd", ["sort -o out.txt in.txt", "sort in.txt out.txt",
                                 "uniq in.txt out.txt", "tree -o out.txt",
                                 "git diff --output=patch.diff", "git log --output=l.txt",
                                 "ls --output=x"])
def test_commands_that_write_a_file_are_not_read_only(cmd):
    assert not _native._readonly_command(cmd)
