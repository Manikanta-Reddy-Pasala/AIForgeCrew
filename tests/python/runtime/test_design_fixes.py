"""Protocol fixes: plain observations, native replay, short prompt, plan handoff."""
from __future__ import annotations

import threading
import types

from aiforge_core.runtime.chat_agent._native import to_native_messages
from aiforge_core.runtime.chat_agent._native_prompt import (
    is_plan_execution,
    native_rules,
    plan_rules,
)
from aiforge_core.runtime.chat_agent._obs_text import render_command, render_observation
from aiforge_core.runtime.chat_agent._prompt_text import BATCH_READS_RULE
from aiforge_core.runtime.chat_agent._tools._schemas import (
    NATIVE_TOOL_NAMES,
    NATIVE_TOOL_SCHEMAS,
    filter_native,
)
from aiforge_core.runtime.doer_tools._tools import call_alias


def test_command_observation_keeps_stderr_when_stdout_is_huge():
    stdout = "o" * 20000
    text = render_command({"ok": False, "code": 1, "stdout": stdout, "stderr": "boom"}, 400)
    assert text.startswith("exit 1\n")
    assert "stderr:\n" in text
    assert "boom" in text


def test_bash_observation_is_plain_command_text():
    text = render_observation(
        "bash", {"ok": False, "code": 2, "stdout": "out", "stderr": "err"}, 400)
    assert text.startswith("exit 2\n")
    assert "stderr:\nerr" in text
    assert "stdout:\nout" in text


def test_file_read_is_raw_lines_not_json():
    text = render_observation("file_read", {"ok": True, "path": "a.py", "content": "x = 1\n"}, 500)
    assert "1|x = 1" in text
    assert '"content"' not in text


def test_web_fetch_stays_json():
    text = render_observation("web_fetch", {"ok": False, "next_step": "stop"}, 500)
    assert text.startswith("{")
    assert "next_step" in text


def test_native_replay_uses_tool_role():
    convo = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "read a.py"},
        {"role": "assistant", "content": 'ACTION: file_read\nARGS_JSON: {"path": "a.py"}'},
        {"role": "user", "content": "OBSERVATION: 1|print(1)"},
    ]
    out = to_native_messages(convo)
    assert out[2]["role"] == "assistant"
    assert out[2]["tool_calls"][0]["function"]["name"] == "file_read"
    assert out[3]["role"] == "tool"
    assert out[3]["content"] == "1|print(1)"


def test_native_rules_have_no_catalog():
    text = native_rules("/tmp")
    assert "Tool arguments:" not in text
    assert "one ACTION per turn" not in text
    assert "ACTION:" not in text
    assert "memory_lookup" in text
    assert "skill_search" not in text
    assert "workflow_search" not in text
    assert "remember_rule" not in text
    assert "Never weaken or delete a test" in text
    assert "Ask only when the choices cannot be undone" in text


def test_batch_reads_rule_does_not_reinstate_one_action():
    assert "one ACTION per turn" not in BATCH_READS_RULE
    assert "jira_read" not in BATCH_READS_RULE
    assert "git_log" not in BATCH_READS_RULE


def test_native_prompt_only_names_given_tools():
    import re
    given = {s["function"]["name"] for s in filter_native(
        NATIVE_TOOL_SCHEMAS, text="what is 2+2")}
    text = native_rules("/tmp") + "\n" + BATCH_READS_RULE
    extras = [name for name in NATIVE_TOOL_NAMES
              if name not in given and re.search(rf"\b{re.escape(name)}\b", text)]
    assert extras == []
    plan_given = {s["function"]["name"] for s in filter_native(
        NATIVE_TOOL_SCHEMAS, mode="plan", text="what is 2+2")}
    plan_extras = [name for name in NATIVE_TOOL_NAMES
                   if name not in plan_given
                   and re.search(rf"\b{re.escape(name)}\b", plan_rules("/tmp"))]
    assert plan_extras == []


def test_plan_rules_are_read_only_and_short():
    text = plan_rules("/tmp")
    assert "read-only" in text.lower()
    assert "Tool arguments:" not in text
    assert "one ACTION per turn" not in text
    assert "numbered plan" in text.lower() or "numbered PLAN" in text


def test_watch_until_is_added_by_cue_not_always():
    bare = {s["function"]["name"] for s in filter_native(
        NATIVE_TOOL_SCHEMAS, text="what is 2+2")}
    cued = {s["function"]["name"] for s in filter_native(
        NATIVE_TOOL_SCHEMAS, text="watch until the port opens")}
    assert "watch_until" not in bare
    assert "watch_until" in cued
    assert "tool_help" in bare
    assert len(bare) < 25
    assert "jira_create" not in bare


def test_plan_execution_marker():
    assert is_plan_execution("Carry out the approved plan.\n\n1. edit a.py")
    assert not is_plan_execution("please edit a.py")


def test_alias_read_dispatches_and_bash_does_not(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("x = 1\n")
    out = call_alias("read", {"path": str(path)})
    assert isinstance(out, dict)
    assert call_alias("bash", {"cmd": "true"}) is None
    assert call_alias("not_a_tool", {}) is None


def test_finish_does_not_ask_to_resend_final():
    from pathlib import Path
    src = Path("aiforge_core/runtime/chat_agent/_turn/_finish.py").read_text()
    assert "resend it unchanged" not in src
    stages = Path("aiforge_core/api/routes/_chat/_stages.py").read_text()
    assert "yield from _integration_verify_events" not in stages


def test_native_system_prompt_is_rules_not_a_catalog(tmp_path):
    from aiforge_core.runtime.chat_agent._turn._convo import _seed_prompt
    _last, _cave, _rules, _prefs, sys_msg = _seed_prompt(
        [{"role": "user", "content": "edit a.py"}], str(tmp_path), False,
        native=True)
    assert "Tool arguments:" not in sys_msg
    assert "one ACTION per turn" not in sys_msg
    assert "codegraph_explore" not in sys_msg
    assert "ensure_runtime" not in sys_msg
    assert "Ask only when the choices cannot be undone" in sys_msg


def test_plan_mode_gets_the_short_prompt(tmp_path):
    from aiforge_core.runtime.chat_agent._turn._convo import _seed_prompt
    _last, _cave, _rules, _prefs, sys_msg = _seed_prompt(
        [{"role": "user", "content": "how should we split auth?"}],
        str(tmp_path), True, native=True, plan_mode=True)
    assert "read-only" in sys_msg.lower()
    assert "Tool arguments:" not in sys_msg
    assert "one ACTION per turn" not in sys_msg


def test_text_plan_mode_keeps_the_catalog(tmp_path):
    from aiforge_core.runtime.chat_agent._turn._convo import _seed_prompt
    _last, _cave, _rules, _prefs, sys_msg = _seed_prompt(
        [{"role": "user", "content": "how should we split auth?"}],
        str(tmp_path), True, native=False, plan_mode=True)
    assert "Tool arguments:" in sys_msg


def test_done_is_emitted_before_a_late_suggestion(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime.chat_agent._turn import _finish as F

    monkeypatch.setattr(F, "_endpoint_one_slot", lambda: False)
    started = {"n": 0}

    def _start(*_a, **_k):
        started["n"] += 1
        return (threading.Event(), threading.Event(), {}, "m", str(tmp_path), 0)

    monkeypatch.setattr(F, "_start_suggestion", _start)
    monkeypatch.setattr(F, "_cancel_suggestion", lambda _h: None)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "hi"}],
        cwd=str(tmp_path), complete_fn=lambda _r, _c: "FINAL: hello"))
    types = [e["type"] for e in evs]
    assert "message" in types and "done" in types
    assert types.index("done") == types.index("message") + 1
    assert not any(e.get("type") == "suggestion" for e in evs)
    assert started["n"] == 1


def test_one_slot_endpoint_skips_the_prediction(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime.chat_agent._turn import _finish as F

    monkeypatch.setattr(F, "_endpoint_one_slot", lambda: True)

    def _boom(*_a, **_k):
        raise AssertionError("prediction must not start on a one-slot endpoint")

    monkeypatch.setattr(F, "_start_suggestion", _boom)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "hi"}],
        cwd=str(tmp_path), complete_fn=lambda _r, _c: "FINAL: hello"))
    assert [e["type"] for e in evs if e["type"] in ("message", "done")][-1] == "done"


def test_green_suite_is_not_rerun_when_the_tree_is_unchanged(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _finish as F
    from aiforge_core.runtime.chat_agent._turn import _outcomes as O

    st = types.SimpleNamespace(last_green_fp="abc", edits_made=1, verify_rounds=0)
    monkeypatch.setattr(O, "content_fingerprint", lambda _cwd: "abc")

    def _must_not_run(*_a, **_k):
        raise AssertionError("suite already passed on this tree")

    monkeypatch.setattr(O, "_run_project_verify", _must_not_run)
    assert list(F._verify_on_final(st, {"text": "done"}, str(tmp_path), False, "")) == []


def test_pause_reads_survive_the_next_message():
    from aiforge_core.runtime.chat_agent._pause import inject, reset, save, take

    reset()
    save(7, [
        {"role": "user", "content": "plan this"},
        {"role": "user", "content": "OBSERVATION: 1|print(1)"},
    ], asked=True)
    nxt = [{"role": "user", "content": "the blue one"}]
    asked = inject(nxt, take(7), plan_mode=True)
    assert asked is True
    assert "Already read" in nxt[0]["content"]
    assert "1|print(1)" in nxt[0]["content"]
    assert take(7) is None
    reset()


def test_spec_gaps_are_fed_to_reconcile():
    from pathlib import Path
    src = Path("aiforge_core/runtime/parallel_subtasks/_stream.py").read_text()
    assert "spec_gaps" in src
    integ = Path("aiforge_core/runtime/parallel_subtasks/_reconcile/_integration.py").read_text()
    assert "spec_gaps" in integ
    assert "SPEC ITEMS STILL MISSING" in integ


def test_local_endpoint_retries_as_a_patch(monkeypatch):
    from aiforge_core.runtime.parallel_subtasks import _runners as R
    from aiforge_core.runtime.parallel_subtasks import _worktree as W

    monkeypatch.delenv("AIFORGE_PARALLEL_SUBTASKS_MAX", raising=False)
    monkeypatch.delenv("AIFORGE_SUBTASK_RETRIES", raising=False)
    monkeypatch.setattr(
        "aiforge_core.llm.router.is_local_endpoint", lambda _role: True)
    assert W._max_workers() == 1
    assert W._retries() == 1
    msg = R._doer_message(
        {"_retry_error": "NameError: tok", "_patch_retry": True},
        "", "a.py", "g")
    assert "Do NOT regenerate" in msg
    assert "patch" in msg.lower()


def test_preempted_learner_is_retried_later(monkeypatch):
    from aiforge_core.api.routes._chat import _history as H

    scheduled = {}

    class _Timer:
        def __init__(self, delay, fn, args=(), kwargs=None):
            scheduled["delay"] = delay
            scheduled["fn"] = fn
            scheduled["args"] = args
            scheduled["kwargs"] = kwargs or {}
            self.daemon = False

        def start(self):
            scheduled["started"] = True

    monkeypatch.setattr("threading.Timer", _Timer)
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_learner.learn_from_chat",
        lambda **_k: {"ok": False, "skipped": "preempted"})
    monkeypatch.setattr(
        "aiforge_core.runtime.preference_capture.capture",
        lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        "aiforge_core.runtime.session_ledger.remember_working_ops",
        lambda *_a, **_k: None)
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_agent._chat_repo_key", lambda _cwd: "repo")
    H._chat_learn_writeback("/tmp", "fix it", "done", [], 1)
    assert scheduled.get("started") is True
    assert scheduled["delay"] == 30.0
    assert scheduled["kwargs"].get("_retry") is True


def test_pipeline_enhancer_stays_2048():
    from pathlib import Path
    src = Path("aiforge_core/runtime/parallel_subtasks/_planning_enhance.py").read_text()
    assert "max_tokens=2048" in src
