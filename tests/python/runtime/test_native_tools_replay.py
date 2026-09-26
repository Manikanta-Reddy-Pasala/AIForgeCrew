"""Native tool list, replay pairing, plan pauses and the post-answer suggestion."""
from __future__ import annotations

import re
import threading
import time

import pytest

from aiforge_core.runtime.chat_agent import _native, _sticky_tools
from aiforge_core.runtime.chat_agent._native_prompt import native_rules
from aiforge_core.runtime.chat_agent._native_replay import to_native_messages
from aiforge_core.runtime.chat_agent._tools._schemas import (
    NATIVE_TOOL_NAMES,
    NATIVE_TOOL_SCHEMAS,
    filter_native,
    native_family_index,
)


def _names(schemas):
    return {s["function"]["name"] for s in schemas}


def _given(text, mode="act", extra=None):
    return _names(filter_native(NATIVE_TOOL_SCHEMAS, mode=mode, text=text,
                                extra=extra))


@pytest.fixture(autouse=True)
def _clean_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    _sticky_tools.forget()
    yield
    _sticky_tools.forget()


# ── cues ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,prefix", [
    ("move ONE-356 to Done", "jira_"),
    ("search the web for the release notes", "web_"),
    ("check the MR pipeline", "gitlab_"),
    ("why did CI fail on the merge request", "gitlab_"),
    ("mail the team the summary", "email_"),
    ("update the design page for auth", "confluence_"),
])
def test_follow_up_wording_adds_its_family(text, prefix):
    assert any(n.startswith(prefix) for n in _given(text))


@pytest.mark.parametrize("text", [
    "fix the UTF-8 decode error", "hash it with SHA-256", "what is 2+2",
    "fix the login page layout",
])
def test_ordinary_words_add_no_integration(text):
    names = _given(text)
    assert not any(n.startswith(("jira_", "confluence_", "gitlab_", "email_"))
                   for n in names)


def test_tool_help_with_a_family_name_adds_the_family():
    convo = [
        {"role": "user", "content": "look at our build"},
        {"role": "assistant", "content":
            'ACTION: tool_help\nARGS_JSON: {"name": "gitlab"}'},
        {"role": "user", "content": 'OBSERVATION: {"ok": true}'},
    ]
    names = _names(_native.select_native_tools(convo, schemas=NATIVE_TOOL_SCHEMAS))
    assert "gitlab_pipelines" in names


def test_a_family_already_called_stays_on_the_list():
    convo = [
        {"role": "user", "content": "and the next one?"},
        {"role": "assistant", "content":
            'ACTION: jira_read\nARGS_JSON: {"key": "X-1"}'},
        {"role": "user", "content": "OBSERVATION: ok"},
    ]
    names = _names(_native.select_native_tools(convo, schemas=NATIVE_TOOL_SCHEMAS))
    assert "jira_transition" in names


def test_families_are_sticky_per_session_and_survive_compaction():
    first = [{"role": "user", "content": "show my jira tickets"}]
    _native.remember_session_tools(first, 42)
    # The message that named Jira was compacted away.
    later = [{"role": "user", "content": "move it to Done"}]
    kept = _names(_native.select_native_tools(
        later, schemas=NATIVE_TOOL_SCHEMAS, session_id=42))
    other = _names(_native.select_native_tools(
        later, schemas=NATIVE_TOOL_SCHEMAS, session_id=43))
    assert "jira_transition" in kept
    assert not any(n.startswith("jira_") for n in other)
    _sticky_tools.forget(42)
    assert _sticky_tools.kept(42) == set()


def test_approved_plan_run_inherits_families_from_the_plan_text():
    convo = [{"role": "user", "content":
              "Carry out the approved plan.\n\n1. Read ONE-356.\n"
              "2. Transition it to Done."}]
    names = _names(_native.select_native_tools(convo, schemas=NATIVE_TOOL_SCHEMAS))
    assert "jira_read" in names


def test_native_call_records_the_session_families(monkeypatch):
    from aiforge_core.llm import client
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-sticky")
    monkeypatch.setattr(client, "complete_raw",
                        lambda *a, **k: {"role": "assistant", "content": "FINAL: ok"})
    fn = _native.make_native_complete_fn(session_id=77)
    fn("chat", [{"role": "user", "content": "mail the team"}])
    assert "email" in _sticky_tools.kept(77)


def test_family_index_lists_families_and_names_no_extra_tool():
    index = native_family_index(NATIVE_TOOL_SCHEMAS, "act")
    assert "- jira:" in index and "- web:" in index and "tool_help" in index
    core = _given("what is 2+2")
    leaked = [n for n in NATIVE_TOOL_NAMES
              if n not in core and re.search(rf"\b{re.escape(n)}\b", index)]
    assert leaked == []
    assert len(index) < 1200


def test_family_index_skips_families_with_nothing_to_add():
    only_core = [s for s in NATIVE_TOOL_SCHEMAS
                 if not s["function"]["name"].startswith(("jira_", "web_"))]
    index = native_family_index(only_core, "act")
    assert "- jira:" not in index and "- web:" not in index


def test_native_rules_do_not_tell_the_model_to_delete_a_failing_test():
    text = native_rules("/tmp")
    assert "delete it." not in text.replace("do not delete it.", "")
    assert "Never weaken or delete a test" in text
    assert "correct that test" in text


# ── replay pairing ────────────────────────────────────────────────────────


def _assert_paired(out):
    for i, msg in enumerate(out):
        for call in msg.get("tool_calls") or []:
            nxt = out[i + 1]
            assert nxt["role"] == "tool" and nxt["tool_call_id"] == call["id"]


def _act(name="file_write", args='{"path": "a.py"}'):
    return {"role": "assistant", "content": f"ACTION: {name}\nARGS_JSON: {args}"}


def test_rejected_call_gets_a_not_run_result():
    out = to_native_messages([
        {"role": "user", "content": "write a.py"},
        _act(),
        {"role": "user", "content":
            "The user REJECTED the `file_write` action and gave this guidance: "
            "use b.py"},
    ])
    _assert_paired(out)
    assert out[2]["content"].startswith("not run: the user rejected")
    assert out[3] == {"role": "user", "content": out[3]["content"]}
    assert "use b.py" in out[3]["content"]


@pytest.mark.parametrize("follow", [
    {"role": "user", "content": "[NEW MESSAGE FROM THE USER — sent while]\nstop"},
    {"role": "user", "content": "[loop guard — not the user] You already ran"},
    {"role": "assistant", "content": "FINAL: done"},
    None,
])
def test_every_call_is_answered(follow):
    convo = [{"role": "user", "content": "go"}, _act("file_read")]
    if follow is not None:
        convo.append(follow)
    out = to_native_messages(convo)
    _assert_paired(out)
    assert out[2]["content"].startswith("not run:")


def test_steer_merged_onto_an_observation_is_a_user_message():
    out = to_native_messages([
        {"role": "user", "content": "go"},
        _act("file_read"),
        {"role": "user", "content":
            "OBSERVATION: 1|x = 1\n\n[NEW MESSAGE FROM THE USER — sent while "
            "you were working.]\nuse postgres"},
    ])
    _assert_paired(out)
    assert out[2]["content"] == "1|x = 1"
    assert out[3]["role"] == "user" and "use postgres" in out[3]["content"]


class _Bad400(Exception):
    code = 400


def test_a_400_retries_with_flattened_history_not_tool_less(monkeypatch):
    from aiforge_core.llm import client
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-strict")
    calls = []

    def _raw(role, messages, tools=None, tool_choice=None):
        calls.append((messages, tools))
        if len(calls) == 1:
            raise _Bad400("HTTP Error 400: Bad Request")
        return {"role": "assistant", "content": "FINAL: ok"}

    def _text(*_a, **_k):
        raise AssertionError("must not fall back to the tool-less call")

    monkeypatch.setattr(client, "complete_raw", _raw)
    monkeypatch.setattr(client, "complete", _text)
    fn = _native.make_native_complete_fn()
    step = fn("chat", [
        {"role": "user", "content": "go"}, _act("file_read"),
        {"role": "user", "content": "OBSERVATION: ok"}])
    assert step.startswith("FINAL")
    retry_msgs, retry_tools = calls[1]
    assert retry_tools
    assert not any(m.get("role") == "tool" or m.get("tool_calls")
                   for m in retry_msgs)


# ── plan pause ────────────────────────────────────────────────────────────


def _saved(asked):
    from aiforge_core.runtime.chat_agent._pause import reset, save, take
    reset()
    save(9, [{"role": "user", "content": "OBSERVATION: " + "x" * 3000},
             {"role": "user", "content": "OBSERVATION: short"}], asked=asked)
    return take(9)


def test_unrelated_follow_up_gets_no_saved_reads():
    from aiforge_core.runtime.chat_agent._pause import inject
    nxt = [{"role": "user", "content": "now explain b.py"}]
    assert inject(nxt, _saved(False)) is False
    assert nxt[0]["content"] == "now explain b.py"


def test_plan_execution_gets_saved_reads_marked_partial():
    from aiforge_core.runtime.chat_agent._pause import inject
    nxt = [{"role": "user", "content": "Carry out the approved plan."}]
    inject(nxt, _saved(False), plan_exec=True)
    body = nxt[0]["content"]
    assert "Already read" in body and "short" in body
    assert "[partial:" in body and "read it again" in body.lower()


def test_planning_after_its_question_keeps_the_reads():
    from aiforge_core.runtime.chat_agent._pause import inject
    nxt = [{"role": "user", "content": "the blue one"}]
    assert inject(nxt, _saved(True), plan_mode=True) is True
    assert "Already read" in nxt[0]["content"]


# ── suggestion after done ─────────────────────────────────────────────────


def test_a_suggestion_ready_shortly_after_done_still_goes_out(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime.chat_agent._turn import _finish as F

    monkeypatch.setattr(F, "_endpoint_one_slot", lambda: False)
    monkeypatch.setattr(F, "_suggest_grace_s", lambda: 5.0)

    class _P:
        def as_event(self):
            return {"type": "suggestion", "id": "p1", "action": "run tests"}

    def _start(*_a, **_k):
        ready, box = threading.Event(), {}

        def _later():
            time.sleep(0.2)
            box["p"] = _P()
            ready.set()
        threading.Thread(target=_later, daemon=True).start()
        return (ready, threading.Event(), box, "m", str(tmp_path), time.monotonic())

    monkeypatch.setattr(F, "_start_suggestion", _start)
    monkeypatch.setattr("aiforge_core.runtime.next_step.remember",
                        lambda *_a, **_k: None, raising=False)
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "hi"}],
        cwd=str(tmp_path), complete_fn=lambda _r, _c: "FINAL: hello"))
    types_ = [e["type"] for e in evs]
    assert types_.index("done") == types_.index("message") + 1
    assert "suggestion" in types_
    assert types_.index("suggestion") > types_.index("done")


# ── doer prompt vs allowlist ──────────────────────────────────────────────


def test_every_tool_the_doer_prompt_names_is_allowed():
    from aiforge_core.config import agent_config
    from aiforge_core.runtime.prompts.doer import PROMPT
    allowed, forbidden = agent_config.allowed_tools_for("doer")
    allowed = set(allowed or ())
    section = PROMPT.split("Tools (use these EXACT names")[1].split(
        "If you call a tool by any other name")[0]
    named = set()
    for line in section.splitlines():
        m = re.match(r"\s*-\s*([a-z_]+(?:/[a-z_]+)*)", line)
        if not m:
            continue
        first, *rest = m.group(1).split("/")
        named.add(first)
        stem = first.rsplit("_", 1)[0] + "_" if "_" in first and rest else ""
        named.update(stem + r for r in rest)
    for extra in re.findall(r"\b([a-z]+_[a-z_]+)\(", section):
        named.add(extra)
    assert named, "no tools parsed from the doer prompt"
    assert named - allowed == set()
    assert not named & set(forbidden)
    body = PROMPT.split("If you call a tool by any other name")[1]
    for name in ("workflow_search", "learn_workflow"):
        if name in body:
            assert name in allowed
