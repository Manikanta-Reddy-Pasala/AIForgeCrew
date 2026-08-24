"""Local-model stall fix: a stuck-loop trip (same action / identical output N
times) is met with a bounded progress-recap NUDGE that lets the model recover
and finish, instead of the old hard bail that abandoned the work."""
from __future__ import annotations

from aiforge_core.runtime.chat_agent import run_chat_agent
from aiforge_core.runtime.chat_agent._context._generation import _progress_recap


def _run(cwd, script_fn, **env):
    import os
    from unittest import mock
    with mock.patch.dict(os.environ, env):
        return list(run_chat_agent([{"role": "user", "content": "read the files"}],
                                   cwd=str(cwd), role="chat", complete_fn=script_fn))


def test_progress_recap_lists_read_files_and_tallies_tools():
    convo = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": 'ACTION: file_read\nARGS_JSON: {"path": "/a/A.java"}'},
        {"role": "assistant", "content": 'ACTION: file_read\nARGS_JSON: {"path": "/b/B.java"}'},
        {"role": "assistant", "content": 'ACTION: file_read\nARGS_JSON: {"path": "/a/A.java"}'},  # dup
        {"role": "assistant", "content": 'ACTION: codegraph_query\nARGS_JSON: {"query": "x"}'},
    ]
    recap = _progress_recap(convo)
    assert "A.java" in recap
    assert "B.java" in recap
    assert "Files already read (2)" in recap        # dup de-duped
    assert "codegraph_query×1" in recap


def test_repeated_action_recovers_then_finishes(tmp_path):
    """The model re-issues the SAME file_read (as the local model does) until the
    loop-guard nudges it; on seeing the nudge it emits FINAL. The run must finish
    with that FINAL — NOT the old 'I keep trying the same step' bail."""
    calls = {"n": 0}
    DUP = 'ACTION: file_read\nARGS_JSON: {"path": "/nope/Same.java"}'

    def script(role, convo):
        # once the loop-guard nudge appears, the model is un-stuck → finish
        if any("[loop guard" in str(m.get("content") or "") for m in convo):
            return "FINAL: read them all, done"
        calls["n"] += 1
        return DUP

    events = _run(tmp_path, script, AIFORGE_CHAT_STUCK_RECOVERIES="3")
    texts = [e.get("text", "") for e in events]
    assert any("↺ repeated" in t for t in texts)              # recovery nudge fired
    assert any("read them all, done" in t for t in texts)     # finished via FINAL
    assert not any("I keep trying the same step" in t for t in texts)  # no hard bail
    # bounded: it recovered without looping forever
    assert calls["n"] <= 6


# Either stuck-guard may fire first depending on repeat pattern (identical output
# → _OUTPUT_REPEAT at 3; interspersed re-reads → _LOOP_REPEAT at 4); both share
# the recovery budget and end in one of these two honest bails.
_BAILS = ("I keep trying the same step", "going in circles")


def test_recovery_is_bounded_then_bails(tmp_path):
    """A model that IGNORES the nudge and keeps repeating still exits — after the
    recovery budget (2) is spent, an honest bail fires. Guard-agnostic."""
    DUP = 'ACTION: file_read\nARGS_JSON: {"path": "/nope/Stuck.java"}'
    events = _run(tmp_path, lambda r, c: DUP, AIFORGE_CHAT_STUCK_RECOVERIES="2")
    texts = [e.get("text", "") for e in events]
    assert sum("↺ repeated" in t for t in texts) == 2         # exactly the budget
    assert any(b in t for t in texts for b in _BAILS)         # then bails
    assert any(e.get("type") == "done" for e in events)


def test_duplicate_read_is_skipped_and_forces_new_progress(tmp_path):
    """A re-read of an already-read file is short-circuited (not re-executed, not
    counted toward the stuck guard) with a 'read a DIFFERENT file / WRITE' nudge —
    so a model that re-reads is pushed to make new progress or finish."""
    f = tmp_path / "A.java"
    f.write_text("class A { int x; }")
    reads = {"n": 0}
    seen_dupe = {"hit": False}

    def script(role, convo):
        # after the duplicate-skip observation appears, the model finishes
        if any("[skipped — duplicate]" in str(m.get("content") or "") for m in convo):
            seen_dupe["hit"] = True
            return "FINAL: done, one file"
        reads["n"] += 1
        return f'ACTION: file_read\nARGS_JSON: {{"path": "{f}"}}'

    events = _run(tmp_path, script, AIFORGE_CHAT_STUCK_RECOVERIES="3")
    texts = [e.get("text", "") for e in events]
    assert any("⏭ duplicate read skipped" in t for t in texts)   # dup detected
    assert seen_dupe["hit"]                                       # model saw the skip
    assert any("done, one file" in t for t in texts)             # finished
    # emitted twice (executed once, then the identical re-read skipped) — only the
    # first actually ran the tool (one `tool` event), the dup never re-executed.
    assert reads["n"] == 2
    tool_reads = [e for e in events if e.get("type") == "tool"
                  and e.get("name") == "file_read"]
    assert len(tool_reads) == 1


def test_recoveries_disabled_restores_hard_bail(tmp_path):
    """AIFORGE_CHAT_STUCK_RECOVERIES=0 restores the old immediate hard bail —
    no recovery nudge, straight to the honest bail."""
    DUP = 'ACTION: file_read\nARGS_JSON: {"path": "/nope/X.java"}'
    events = _run(tmp_path, lambda r, c: DUP, AIFORGE_CHAT_STUCK_RECOVERIES="0")
    texts = [e.get("text", "") for e in events]
    assert not any("↺ repeated" in t for t in texts)
    assert any(b in t for t in texts for b in _BAILS)
