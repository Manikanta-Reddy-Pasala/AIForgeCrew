"""End-to-end re-verification of the issues the owner reported.

Every scenario drives the REAL loop (``run_chat_agent`` / the chat pipeline)
with a scripted model (``complete_fn``) or stub stage runners. Nothing here
unit-tests a helper. The suite-wide safe defaults in ``tests/python/conftest.py``
(persist forever -> stop, pause-on-stuck, no plateau replans, ...) are removed by
the autouse fixture below so each scenario runs the PRODUCTION defaults.

Every scenario is bounded: the scripted model counts its calls and, past a cap,
answers a sentinel FINAL so a bug that would run unbounded FAILS instead of
hanging the suite.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime import chat_cancel, run_interrupt

CAP_SENTINEL = "SENTINEL-CAP-REACHED"


@pytest.fixture(autouse=True)
def _production_defaults(monkeypatch):
    """Undo conftest's test-only defaults: production behaviour."""
    for name in ("AIFORGE_CHAT_PERSIST_S", "AIFORGE_CHAT_PAUSE_ON_STUCK",
                 "AIFORGE_PLATEAU_REPLANS", "AIFORGE_PIPELINE_PERSIST_S",
                 "AIFORGE_LLM_WAIT_MAX_S", "AIFORGE_CHAT_STUCK_ESCALATIONS",
                 "AIFORGE_CHAT_IDENTICAL_REPEATS", "AIFORGE_CHAT_LOOP_BACKSTOP",
                 "AIFORGE_CHAT_MAX_RECOVERIES", "AIFORGE_CHAT_STUCK_RESTART",
                 "AIFORGE_STUCK_REASON_STEPS", "AIFORGE_CHAT_LLM_RETRIES"):
        monkeypatch.delenv(name, raising=False)
    # the model wait's default is also pinned low by conftest
    from aiforge_core.llm import model_wait
    monkeypatch.setattr(model_wait, "_DEFAULT_MAX_S", 0, raising=False)
    # no real sleeping anywhere a retry/outage wait pauses (Stop still works)
    def _pause(secs, session_id=None, slice_s=None):
        if session_id is not None and chat_cancel.is_cancelled(session_id):
            return "stop"
        return None
    monkeypatch.setattr(run_interrupt, "pause", _pause)
    yield


def run(messages, cwd, fn, cap_events=4000, **kw):
    """Drive the real loop; a runaway is cut by ``cap_events`` and fails."""
    evs = []
    gen = ca.run_chat_agent(messages, cwd=str(cwd), complete_fn=fn, **kw)
    try:
        for ev in gen:
            evs.append(ev)
            assert len(evs) < cap_events, "run did not stop (event cap hit)"
    finally:
        gen.close()
    return evs


def messages_of(evs):
    return [e for e in evs if e.get("type") == "message"]


def last_text(convo, n=3):
    return "\n".join(str(m.get("content")) for m in convo[-n:])


class Model:
    """A scripted model with a hard call cap."""

    def __init__(self, script, cap=300):
        self.script, self.cap, self.n = script, cap, 0
        self.convos = []

    def __call__(self, role, convo):
        self.n += 1
        self.convos.append([dict(m) for m in convo])
        if self.n > self.cap:
            return f"FINAL: {CAP_SENTINEL}"
        return self.script(self.n, role, convo)

    @property
    def capped(self):
        return self.n > self.cap


def git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


# ════════════════════════════════════════════════════════════════════════════
# 1. `run_command rm -f <path>` repeated dozens of times
# ════════════════════════════════════════════════════════════════════════════

def _rm_script(repo, victim, obey=None):
    """The model repeats `rm -f victim` (each OK), the wording varies and the
    workspace moves (an unrelated log file is rewritten every call)."""
    def script(n, role, convo):
        (repo / "service.log").write_text(f"tick {n}\n")       # fingerprint moves
        tail = last_text(convo)
        if obey and obey(tail):
            return "FINAL: removed the stale config; nothing else needed."
        return (f"THOUGHT: attempt {n}, maybe it is still there ({n * 7})\n"
                f"ACTION: run_command\nARGS_JSON: {json.dumps({'cmd': f'rm -f {victim}'})}")
    return script


def test_1_rm_repeat_is_broken_out_of_and_ends_with_a_summary(tmp_path):
    repo = git_repo(tmp_path / "w")
    victim = repo / "preprocessed.cfg"
    # The model obeys only the final "write FINAL" instruction.
    m = Model(_rm_script(repo, victim, obey=lambda t: "Write `FINAL:` now" in t))
    evs = run([{"role": "user", "content": "clear the preprocessed configs"}],
              repo, m)
    msgs = messages_of(evs)
    assert not m.capped, f"unbounded: {m.n} calls"
    assert msgs and CAP_SENTINEL not in msgs[-1]["text"]
    assert "stale config" in msgs[-1]["text"]
    assert m.n < 120, m.n
    assert not any(e.get("awaiting_input") for e in evs)


def test_1_rm_repeat_with_a_model_that_never_obeys_still_ends(tmp_path):
    repo = git_repo(tmp_path / "w")
    m = Model(_rm_script(repo, repo / "preprocessed.cfg"))        # never obeys
    evs = run([{"role": "user", "content": "clear the preprocessed configs"}],
              repo, m)
    msgs = messages_of(evs)
    assert not m.capped, f"unbounded: {m.n} calls"
    assert msgs and CAP_SENTINEL not in msgs[-1]["text"]
    assert m.n < 200, m.n


def test_1_rm_repeat_switches_approach_when_nudged(tmp_path):
    repo = git_repo(tmp_path / "w")
    victim = repo / "preprocessed.cfg"
    seen_nudge = {"n": 0}

    def script(n, role, convo):
        (repo / "service.log").write_text(f"tick {n}\n")
        if "[loop guard" in last_text(convo) or "HANDOFF" in last_text(convo):
            seen_nudge["n"] += 1
            return "FINAL: the file is already gone; confirmed with ls."
        return f'ACTION: run_command\nARGS_JSON: {json.dumps({"cmd": f"rm -f {victim}"})}'
    m = Model(script)
    evs = run([{"role": "user", "content": "clear the preprocessed configs"}], repo, m)
    msgs = messages_of(evs)
    assert not m.capped and "already gone" in msgs[-1]["text"]
    assert m.n <= 25, m.n


def test_1_through_the_handoff_restart_and_reasoning_boost(tmp_path):
    """The restart (convo replaced) and the stuck reasoning boost both happen
    inside the run, and the counters neither re-trip at once nor never trip."""
    from aiforge_core.llm import reasoning
    repo = git_repo(tmp_path / "w")
    victim = repo / "preprocessed.cfg"
    boosted_calls, restarts_seen = [], []

    def script(n, role, convo):
        boosted_calls.append(reasoning.boosted())
        if any("HANDOFF" in str(m.get("content")) for m in convo):
            restarts_seen.append(n)
        (repo / "service.log").write_text(f"tick {n}\n")
        if "Write `FINAL:` now" in last_text(convo):
            return "FINAL: gave up cleanly; the path is already removed."
        return f'ACTION: run_command\nARGS_JSON: {json.dumps({"cmd": f"rm -f {victim}"})}'
    m = Model(script)
    evs = run([{"role": "user", "content": "clear the preprocessed configs"}], repo, m)
    msgs = messages_of(evs)
    assert not m.capped, f"unbounded: {m.n}"
    assert msgs and "gave up cleanly" in msgs[-1]["text"]
    assert any(boosted_calls), "stuck reasoning boost never reached the model call"
    assert restarts_seen, "no handoff restart happened"
    # after a restart the counters are neither stale-tripped nor dead: the number
    # of repeated calls between two consecutive change-of-approach trips is
    # small but > 0 (re-trips at once = counters not reset; huge = never trips)
    trips = [i for i, e in enumerate(evs)
             if e.get("type") == "thought" and "changing approach" in e.get("text", "")]
    assert len(trips) >= 4, trips
    gaps = []
    for a, b in zip(trips, trips[1:]):
        gaps.append(sum(1 for e in evs[a:b] if e.get("type") == "tool"))
    import re as _re
    nums = [int(_re.search(r"try (\d+)", evs[i]["text"]).group(1)) for i in trips]
    # even-numbered trips are the ones that restart from a handoff: the run that
    # follows one must get to work before it can trip again
    after_restart = [g for g, n_ in zip(gaps, nums) if n_ % 2 == 0]
    assert after_restart and min(after_restart) >= 1, (gaps, nums)
    assert max(gaps) <= 12, gaps         # and it keeps tripping (never "never trips")
    assert m.n < 150

# ════════════════════════════════════════════════════════════════════════════
# 2. the model copies the system's `[did: …]` action log as its answer
# ════════════════════════════════════════════════════════════════════════════

DID = "[did: file_read(app.py)✓, grep(read_path)✓, run_command(pytest -q)✓]"


def _chat_with_did_history(goal):
    # exactly what api/routes/_chat/_history.py builds for earlier turns
    return [
        {"role": "user", "content": "look at the read path"},
        {"role": "assistant", "content": f"Looked at it.\n{DID}"},
        {"role": "user", "content": goal},
    ]


@pytest.mark.parametrize("copy", [
    f"FINAL: Honest re-check — verifying what is actually on disk:\n{DID}",
    f"FINAL: {DID}",
    DID,                                          # implicit prose, no FINAL:
    "THOUGHT: done\nFINAL: Re-verified.\n[did: file_read(a)✓,\n file_read(b)✓]",
])
def test_2_log_copy_is_stripped_and_sent_back_to_work(tmp_path, copy):
    (tmp_path / "app.py").write_text("def read_path():\n    return 1\n")
    answered = {"n": 0}

    def script(n, role, convo):
        if "only a log of earlier actions" in str(convo[-1].get("content")):
            answered["n"] += 1
            return "FINAL: read_path returns 1 in app.py; the check passes."
        return copy
    m = Model(script, cap=20)
    evs = run(_chat_with_did_history("is the read path still correct?"), tmp_path, m)
    texts = [e["text"] for e in messages_of(evs)]
    assert texts and all("[did:" not in t for t in texts)
    assert "read_path returns 1" in texts[-1]
    assert answered["n"] >= 1                      # it WAS sent back to work
    assert not m.capped


def test_2_a_model_that_only_ever_copies_the_log_never_ends_with_one(tmp_path):
    m = Model(lambda n, r, c: f"FINAL: Re-check:\n{DID}", cap=40)
    evs = run(_chat_with_did_history("is the read path still correct?"), tmp_path, m)
    texts = [e["text"] for e in messages_of(evs)]
    assert texts and all("[did:" not in t for t in texts)
    assert not m.capped and m.n <= 8
    assert "could not produce a result" in texts[-1]


def test_2_a_real_answer_with_a_trailing_log_keeps_the_answer(tmp_path):
    m = Model(lambda n, r, c: f"FINAL: The query takes 480 ms for 50k rows.\n{DID}", cap=10)
    evs = run(_chat_with_did_history("how fast is the read?"), tmp_path, m)
    texts = [e["text"] for e in messages_of(evs)]
    assert len(texts) == 1 and "480 ms" in texts[0] and "[did:" not in texts[0]
    assert m.n == 1                                # no needless extra round trip


def test_2_strict_finish_doer_run_also_strips_the_log(tmp_path):
    m = Model(lambda n, r, c: (
        "FINAL: patched and verified." if "only a log" in str(c[-1].get("content"))
        else f"FINAL: Done:\n{DID}"), cap=20)
    evs = run([{"role": "user", "content": "check the read path"}], tmp_path, m,
              strict_finish=True, role="doer")
    texts = [e["text"] for e in messages_of(evs)]
    assert texts and "[did:" not in texts[-1] and not m.capped



# ════════════════════════════════════════════════════════════════════════════
# 3. model not responding / Stop / context overflow
# ════════════════════════════════════════════════════════════════════════════

def _flaky(fail_with, n_fail, then, cap=400):
    state = {"calls": 0}

    def fn(role, convo):
        state["calls"] += 1
        if state["calls"] > cap:
            return f"FINAL: {CAP_SENTINEL}"
        if state["calls"] <= n_fail:
            raise fail_with()
        return then(state["calls"], convo)
    return fn, state


@pytest.mark.parametrize("exc", [
    lambda: RuntimeError("HTTP 500 internal server error"),
    lambda: TimeoutError("timed out"),
    lambda: ConnectionRefusedError("[Errno 111] Connection refused"),
    lambda: RuntimeError("HTTP 503 service unavailable"),
])
def test_3_a_model_that_fails_many_calls_then_answers_completes_the_task(tmp_path, exc):
    fn, st = _flaky(exc, 30, lambda n, c: "FINAL: the answer is 42.")
    evs = run([{"role": "user", "content": "what is the answer?"}], tmp_path, fn)
    texts = [e["text"] for e in messages_of(evs)]
    assert texts and texts[-1] == "the answer is 42.", texts
    assert st["calls"] == 31
    assert not any(e.get("type") == "stopped" for e in evs)
    assert not any("didn't respond" in t or "stopped responding" in t for t in texts)


def test_3_failures_in_the_middle_of_work_resume_the_task(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    state = {"n": 0}

    def fn(role, convo):
        state["n"] += 1
        if 3 <= state["n"] <= 22:                 # outage after the first tool call
            raise RuntimeError("HTTP 500 internal server error")
        if state["n"] == 1:
            return 'ACTION: file_read\nARGS_JSON: {"path": "a.txt"}'
        if state["n"] > 100:
            return f"FINAL: {CAP_SENTINEL}"
        return "FINAL: a.txt says hello."
    evs = run([{"role": "user", "content": "read a.txt and tell me"}], tmp_path, fn)
    texts = [e["text"] for e in messages_of(evs)]
    assert texts[-1] == "a.txt says hello."
    assert any(e.get("type") == "tool" and e["name"] == "file_read" for e in evs)


def test_3_stop_ends_a_run_whose_model_never_answers(tmp_path):
    sid = 910001
    chat_cancel.start(sid)
    calls = {"n": 0}

    def fn(role, convo):
        calls["n"] += 1
        if calls["n"] == 12:                       # the user presses Stop
            chat_cancel.cancel(sid)
        if calls["n"] > 300:
            return f"FINAL: {CAP_SENTINEL}"
        raise RuntimeError("HTTP 500 internal server error")
    try:
        evs = run([{"role": "user", "content": "do it"}], tmp_path, fn, session_id=sid)
    finally:
        chat_cancel.finish(sid)
    assert calls["n"] < 300
    assert evs[-1]["type"] == "done"
    assert any(e.get("type") == "error" and "stopped by user" in e.get("text", "")
               for e in evs)
    assert not any(CAP_SENTINEL in e.get("text", "") for e in messages_of(evs))


def test_3_stop_ends_a_run_during_an_outage_wait(tmp_path):
    sid = 910002
    chat_cancel.start(sid)
    calls = {"n": 0}

    def fn(role, convo):
        calls["n"] += 1
        if calls["n"] == 6:
            chat_cancel.cancel(sid)
        if calls["n"] > 300:
            return f"FINAL: {CAP_SENTINEL}"
        raise ConnectionRefusedError("[Errno 111] Connection refused")
    try:
        evs = run([{"role": "user", "content": "do it"}], tmp_path, fn, session_id=sid)
    finally:
        chat_cancel.finish(sid)
    assert calls["n"] < 300 and evs[-1]["type"] == "done"
    assert any("stopped by user" in e.get("text", "") for e in evs
               if e.get("type") == "error")


def test_3_context_overflow_twice_restarts_from_a_handoff(tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text(f"file {i}\n" * 20)
    state = {"n": 0, "after": None}

    def fn(role, convo):
        state["n"] += 1
        n = state["n"]
        if n <= 3:                                  # three reads of real work first
            return f'ACTION: file_read\nARGS_JSON: {{"path": "f{n - 1}.txt"}}'
        if n in (4, 5, 6):                          # then the prompt "does not fit", repeatedly
            if "HANDOFF" in "\n".join(str(m.get("content")) for m in convo):
                state["after"] = [dict(m) for m in convo]
                return "FINAL: continued from the handoff; all three files read."
            raise RuntimeError("This model's maximum context length is 4096 tokens; "
                               "your request exceeds the context window")
        if n > 60:
            return f"FINAL: {CAP_SENTINEL}"
        state["after"] = state["after"] or [dict(m) for m in convo]
        return "FINAL: ok"
    evs = run([{"role": "user", "content": "read f0 f1 f2 and summarise them"}],
              tmp_path, fn)
    texts = [e["text"] for e in messages_of(evs)]
    assert texts and "continued from the handoff" in texts[-1], texts
    assert state["after"] is not None
    flat = "\n".join(str(m.get("content")) for m in state["after"])
    assert "HANDOFF" in flat and "read f0 f1 f2" in flat   # the saved state carries the goal
    assert any("restarted from a handoff" in e.get("text", "") for e in evs
               if e.get("type") == "thought")



# ════════════════════════════════════════════════════════════════════════════
# 4. a big multi-part request in simple chat
# ════════════════════════════════════════════════════════════════════════════

BIG_REQUEST = (
    "Please do all of these:\n"
    "1. create alpha.txt containing the word ALPHA\n"
    "2. create beta.txt containing the word BETA\n"
    "3. create gamma.txt containing the word GAMMA\n"
    "4. create delta.txt containing the word DELTA\n")


def _plan(slug, **kw):
    return f"ACTION: plan_progress\nARGS_JSON: {json.dumps({'slug': slug, **kw})}"


def _write(path, content):
    return ("ACTION: file_write\nARGS_JSON: "
            + json.dumps({"path": path, "content": content}))


def _flat(convo):
    return "\n".join(str(m.get("content")) for m in convo)


def test_4_big_request_is_decomposed_gated_reset_and_finalised_last(tmp_path):
    names = ["alpha.txt", "beta.txt", "gamma.txt", "delta.txt"]
    words = {"alpha.txt": "ALPHA", "beta.txt": "BETA", "gamma.txt": "GAMMA",
             "delta.txt": "DELTA"}
    phase = {"i": 0, "step": "write", "premature": False}
    seen = []        # (call n, flat prompt) of the first step for each item

    def script(n, role, convo):
        i = phase["i"]
        if i >= len(names):
            return "FINAL: all four files created."
        f = names[i]
        if phase["step"] == "write":
            seen.append((i, [dict(m) for m in convo]))
            # a premature FINAL after item 1 is attempted once
            if i == 1 and not phase["premature"]:
                phase["premature"] = True
                return "FINAL: done, everything is created."
            phase["step"] = "close"
            return _write(f, words[f])
        phase["step"] = "write"
        phase["i"] += 1
        return _plan(f"part-{i + 1}", status="done")
    m = Model(script, cap=80)
    evs = run([{"role": "user", "content": BIG_REQUEST}], tmp_path, m)
    # decomposition: the dock got the parts up front
    dock = [e for e in evs if e.get("type") == "subtasks"]
    assert dock and len(dock[0]["items"]) == 4
    assert [it["slug"] for it in dock[0]["items"]] == [f"part-{i}" for i in range(1, 5)]
    # the work landed
    for f in names:
        assert (tmp_path / f).read_text() == words[f]
    # per-item context reset: item 2's first prompt has no transcript of item 1
    # but has its result note and the board
    item2 = [c for i, c in seen if i == 2][0]
    flat2 = _flat(item2)
    assert "ALPHA" not in "\n".join(str(x.get("content")) for x in item2
                                    if x["role"] == "assistant")
    assert "file_write" not in "\n".join(str(x.get("content")) for x in item2[1:]
                                         if "ACTION" in str(x.get("content")))
    assert "alpha.txt" in flat2                      # its result note
    assert "AIFORGE_TASK_BOARD" in flat2             # the board
    assert [m_["role"] for m_ in item2][:2] == ["system", "user"]
    # the FINAL only after the items are closed: the premature one was not accepted
    msgs = messages_of(evs)
    assert len(msgs) == 1 and "all four files created" in msgs[0]["text"]
    idx_final = evs.index(msgs[0])
    closed = [k for k, e in enumerate(evs)
              if e.get("type") == "tool" and e.get("name") == "plan_progress"
              and e["result"].get("status") == "done"]
    assert len(closed) == 4 and max(closed) < idx_final
    assert not m.capped


def test_4_an_item_closed_without_evidence_is_rejected_by_the_gate(tmp_path):
    """The model claims every item done without doing anything: nothing is
    silently 'done'."""
    names = ["alpha.txt", "beta.txt", "gamma.txt", "delta.txt"]
    state = {"i": 0}
    rejected = []

    def script(n, role, convo):
        last = str(convo[-1].get("content"))
        if "not accepted as done" in last:
            rejected.append(last)
        if state["i"] >= 4:
            return "FINAL: stopping."
        state["i"] += 1
        return _plan(f"part-{state['i']}", status="done")
    m = Model(script, cap=60)
    evs = run([{"role": "user", "content": BIG_REQUEST}], tmp_path, m)
    assert rejected, "an item with no evidence was accepted silently"
    boards = [e for e in evs if e.get("type") == "tool" and e["name"] == "plan_progress"
              and e["result"].get("ok") is False]
    assert boards and "nothing shows" in boards[0]["result"]["error"]
    assert not m.capped


def test_4_a_stuck_item_is_retried_and_the_run_still_finishes(tmp_path):
    """Item 2's write keeps failing the same way (no edit lands, same call
    repeated). The loop guard escalates / restarts from a handoff; the model
    then takes another approach and the run finishes with all items closed."""
    names = ["alpha.txt", "beta.txt", "gamma.txt", "delta.txt"]
    words = dict(zip(names, ["ALPHA", "BETA", "GAMMA", "DELTA"]))
    st = {"i": 0, "step": "write", "stuck_calls": 0}

    def script(n, role, convo):
        i = st["i"]
        if i >= 4:
            return "FINAL: all four done, beta after changing approach."
        f = names[i]
        flat = _flat(convo[-2:])
        if st["step"] == "write":
            if i == 1 and not ("[loop guard" in _flat(convo) or "HANDOFF" in _flat(convo)):
                st["stuck_calls"] += 1          # keeps trying a no-op listing
                return 'ACTION: run_command\nARGS_JSON: {"cmd": "ls nonexistent_dir"}'
            st["step"] = "close"
            return _write(f, words[f])
        st["step"] = "write"
        st["i"] += 1
        return _plan(f"part-{i + 1}", status="done")
    m = Model(script, cap=150)
    evs = run([{"role": "user", "content": BIG_REQUEST}], tmp_path, m)
    assert st["stuck_calls"] >= 3                      # it really was stuck
    assert not m.capped
    for f in names:
        assert (tmp_path / f).exists(), f
    msgs = messages_of(evs)
    assert msgs and "all four done" in msgs[-1]["text"]
    assert any("changing approach" in e.get("text", "") or "recap + nudge" in e.get("text", "")
               for e in evs if e.get("type") == "thought")



# ════════════════════════════════════════════════════════════════════════════
# HTTP-level harness: the REAL chat route + producer + routing + loop, with one
# seam stubbed — the model transport (llm.client._complete_impl) — and, for team
# turns, the ADK Runner (so stages are scripted events).
# ════════════════════════════════════════════════════════════════════════════

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def trees(repo):
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]


def _adk_event(author, text):
    import types as t
    return t.SimpleNamespace(
        author=author, partial=False,
        content=t.SimpleNamespace(parts=[t.SimpleNamespace(
            text=text, function_call=None, function_response=None)]),
        node_info=None, actions=None)


class Api:
    """Handle on the running app and the scripted seams."""

    def __init__(self, client, repo, store):
        self.client, self.repo, self.store = client, repo, store
        self.agent = lambda n, role, convo: "FINAL: ok"       # roles chat / doer
        self.other = lambda role, messages: "code_build"      # classifier etc.
        self.calls = []                                       # (role, convo)
        self.adk = {"events": [], "raise": None, "on_run": None, "prompts": []}

    def complete(self, role, messages, **kw):
        self.calls.append((role, [dict(m) for m in messages]))
        if role in ("chat", "doer"):
            return self.agent(sum(1 for r, _ in self.calls if r in ("chat", "doer")),
                              role, messages)
        return self.other(role, messages)

    def session(self, title="t"):
        return self.client.post("/api/chat/sessions",
                                json={"title": title, "cwd": str(self.repo)}).json()["id"]

    def send(self, sid, content, mode="simple", **extra):
        r = self.client.post(f"/api/chat/sessions/{sid}/message",
                             json={"content": content, "mode": mode, **extra})
        assert r.status_code == 200, r.text
        evs = [json.loads(ln[5:]) for ln in r.text.splitlines() if ln.startswith("data:")]
        self.settle(sid)
        return evs

    def settle(self, sid, timeout=30.0):
        from aiforge_core.runtime import chat_runs
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = chat_runs.get(sid)
            if run is None or run.done:
                return
            time.sleep(0.05)
        raise AssertionError("the run never finished")


@pytest.fixture
def api(monkeypatch, tmp_path):
    import importlib
    for k, v in {
        "AIFORGE_CONFIG_DIR": str(tmp_path / "cfg"), "AIFORGE_DB_PATH": str(tmp_path / "api.db"),
        "AIFORGE_CHAT_DB_PATH": str(tmp_path / "chat.db"),
        "AIFORGE_CHAT_WORKSPACE_ROOT": str(tmp_path / "ws"),
        "AIFORGE_MEMORY_DB_PATH": str(tmp_path / "memory.db"),
        "AIFORGE_MEMORY_MD_DIR": str(tmp_path / "cfg" / "memory"),
        "AIFORGE_CHAT_AUTO_MEMORY": "0", "AIFORGE_CHAT_TITLE": "0",
        "AIFORGE_CHAT_SUMMARY": "0", "AIFORGE_CHAT_LEARNER": "0",
        "AIFORGE_LLM_MAX_RPM": "0", "AIFORGE_CHAT_TOOL_PROTOCOL": "text",
        "AIFORGE_PROJECT_INGEST": "0", "AIFORGE_SESSION_COMPACT_ON_SWITCH": "0",
        "AIFORGE_CHAT_NEXT_STEP": "0", "AIFORGE_PREDICT_NEXT_STEP": "0",
    }.items():
        monkeypatch.setenv(k, v)
    for k in ("AIFORGE_PG_URL", "AIFORGE_FORCE_PG", "AIFORGE_PARALLEL_SUBTASKS",
              "AIFORGE_PARALLEL_SUBTASKS_MAX", "AIFORGE_BEST_OF_N",
              "AIFORGE_SHARED_WORKTREE", "AIFORGE_MEMORY_BACKEND",
              "AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    root = tmp_path / "repos"
    repo = root / "shop"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "cart.py").write_text("def total(x):\n    return sum(x)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setenv("AIFORGE_PROJECTS_ROOT", str(root))
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as tstore
    importlib.reload(tstore)
    import aiforge_core.api.api as apimod
    importlib.reload(apimod)
    from aiforge_core.config import repo_map
    monkeypatch.setattr(repo_map, "_load", lambda: {})
    from aiforge_core.memory import projects
    monkeypatch.setattr(projects, "_BOOT_REPO_ROOT", "")
    projects.forget_scan()
    from aiforge_core.runtime import chat_store, repo_ident
    chat_store.reset_backend_for_tests()
    repo_ident._GIT_TOPLEVEL_CACHE.clear()
    from fastapi.testclient import TestClient
    a = Api(TestClient(apimod.app), repo, chat_store)
    from aiforge_core.llm import client as llm_client
    monkeypatch.setattr(llm_client, "_complete_impl", a.complete)
    # the ADK team: scripted stage events through a stub Runner
    import google.adk.runners as runners

    import aiforge_core.runtime.pipeline as pl
    monkeypatch.setattr(pl, "build_pipeline", lambda **kw: object())

    class _Runner:
        def __init__(self, **kw):
            pass

        def run_async(self, **kw):
            try:
                a.adk["prompts"].append(kw["new_message"].parts[0].text)
            except Exception:  # noqa: BLE001
                pass

            async def gen():
                if a.adk["on_run"]:
                    a.adk["on_run"]()
                for e in a.adk["events"]:
                    yield e
                if a.adk["raise"]:
                    raise a.adk["raise"]
            return gen()

        async def close(self):
            pass

    monkeypatch.setattr(runners, "Runner", _Runner)
    yield a
    # Session ids restart at 1 with every fresh db, so a run (or cancel token)
    # still registered under id 1 would answer the NEXT scenario's first message
    # with 409 "a run is already in progress". End whatever this one left.
    from aiforge_core.runtime import chat_runs
    chat_cancel.cancel_all()
    for _sid in chat_runs.finish_all():
        a.settle(_sid, timeout=10.0)
    chat_runs._RUNS.clear()
    # cancel_all() leaves each token registered AND cancelled; a later test that
    # reuses one of those session ids (run_chat_agent called directly, which
    # does not start a fresh token) would then see "cancelled" and stop at once.
    for _sid in list(chat_cancel.active_sessions()):
        chat_cancel.finish(_sid)
    chat_store.reset_backend_for_tests()


def _wait_assistant_turns(store, sid, n, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = store.get_messages(sid)
        if sum(1 for r in rows if r.get("role") == "assistant") >= n:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"assistant turn {n} never persisted: {store.get_messages(sid)}")


# ════════════════════════════════════════════════════════════════════════════
# 5. Simple -> Team in the same chat, with stale state
# ════════════════════════════════════════════════════════════════════════════

ASK5 = ("implement a rust and go based parallel read path, target 500 ms for 50k "
        "records")


def _simple_turn_with_stale_state(api, sid):
    """Turn 1 (simple): a 3-item board closed 'done', a tool call (-> [did:]
    digest in history) and an answer claiming everything is done."""
    steps = [
        _plan("a", title="design"), _plan("b", title="parallel reader"),
        _plan("c", title="benchmark"),
        _write("notes.txt", "x"), _plan("a", status="done"),
        _write("reader.txt", "x"), _plan("b", status="done"),
        _write("bench.txt", "x"), _plan("c", status="done"),
        "FINAL: ~80% built; 3/3 tasks done, the parallel read path is complete.",
    ]
    api.agent = lambda n, role, convo: steps[min(n - 1, len(steps) - 1)]
    evs = api.send(sid, "check the read path and set up the plan", mode="simple")
    _wait_assistant_turns(api.store, sid, 1)
    return evs


def test_5_team_request_after_a_simple_chat_still_plans_and_runs_the_doer(api):
    sid = api.session()
    _simple_turn_with_stale_state(api, sid)
    rows = api.store.get_messages(sid)
    assert any("[did:" in str(m.get("content")) or m.get("steps") for m in rows)
    # the team turn: planner + doer run (the stub creates the Doer's edit)
    def doer_edits():
        wd = api.store.get_session(sid)["workdir"] or str(api.repo)
        (os.path.join(wd, "reader.rs"))
        with open(os.path.join(wd, "reader.rs"), "w") as fh:
            fh.write("// parallel reader\n")
    api.adk["on_run"] = doer_edits
    api.adk["events"] = [_adk_event("planner", '{"plan": "p"}'),
                         _adk_event("doer", "implemented reader.rs")]
    evs = api.send(sid, ASK5, mode="team")
    # the pipeline ran (it was not downgraded to the single agent) and was told
    # the request is to be EXECUTED, with the stale 'done' claims marked unverified
    assert len(api.adk["prompts"]) == 1, "the Team request did not reach the pipeline"
    prompt = api.adk["prompts"][0]
    from aiforge_core.runtime import chat_pipeline_prompt as PP
    assert PP.EXECUTE_DIRECTIVE in prompt
    assert "claims that work is done are unverified" in prompt
    assert ASK5 in prompt
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs and "implemented reader.rs" in msgs[-1]
    assert any(e.get("type") == "changes" for e in evs)
    assert not any(e.get("type") == "stopped" for e in evs)


def test_5_pipeline_that_never_reaches_the_doer_runs_the_doer_on_the_plan(api):
    sid = api.session()
    _simple_turn_with_stale_state(api, sid)
    # the live failure: the team run ends after the researcher (stale state made
    # it believe the work was done) and nothing is implemented
    api.adk["events"] = [_adk_event(
        "researcher", "Bottom line for the Planner: the parallel architecture is ~80% "
                      "built on disk, the real work is (a) (b) (c)")]
    wrote = {}

    def doer(n, role, convo):
        flat = _flat(convo)
        if "NOTHING has been changed yet" in flat and "wrote" not in wrote:
            wrote["wrote"] = True
            return _write("reader.rs", "// parallel reader\n")
        return "FINAL: implemented reader.rs from the plan."
    api.agent = doer
    evs = api.send(sid, ASK5, mode="team")
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs and "implemented reader.rs" in msgs[-1]
    assert not any("Bottom line for the Planner" in m for m in msgs)
    assert any("implementation step" in e.get("text", "") for e in evs
               if e.get("type") == "thought")
    assert any(e.get("type") == "changes" and any(
        f["path"].endswith("reader.rs") for f in e["files"]) for e in evs)
    assert not any(e.get("type") == "stopped" for e in evs)


def test_5_when_no_doer_ran_and_nothing_changes_the_message_says_so(api):
    sid = api.session()
    _simple_turn_with_stale_state(api, sid)
    api.adk["events"] = [_adk_event("researcher", "Bottom line: it is ~80% built")]
    api.agent = lambda n, role, convo: "FINAL: I reviewed the code; it looks fine."
    evs = api.send(sid, ASK5, mode="team")
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    final = msgs[-1]
    assert final.startswith("(stopped)") and "Nothing was implemented" in final
    assert "researcher stage" in final
    assert not any(m == "Bottom line: it is ~80% built" for m in msgs)
    assert {"type": "stopped", "reason": "no_implementation"} in evs


def test_5_an_implement_request_in_simple_mode_never_ends_as_research_text(api):
    """A plan with no edit is put to the working model once, in its own
    context ("you changed no file: did the message ask for work?"). A model
    that then says the change was not made has its answer labelled, so a plan
    is not read as the implementation."""
    sid = api.session()
    seen = []

    def agent(n, role, convo):
        seen.append(str(convo[-1].get("content")))
        if seen[-1].startswith("[harness — not the user] A routine check"):
            return "FINAL: NOT DONE: I only worked out the plan: (a) (b) (c)."
        return "FINAL: Bottom line: the real work is (a) (b) (c)."
    api.agent = agent
    evs = api.send(sid, ASK5, mode="simple")
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert sum(s.startswith("[harness — not the user] A routine check")
               for s in seen) == 1                      # asked once
    assert msgs and msgs[-1].startswith("(No file was changed in this turn")
    assert "analysis or a plan, not an implementation" in msgs[-1]
    assert msgs[-1].endswith("I only worked out the plan: (a) (b) (c).")


def test_5_a_model_sent_back_by_the_check_does_the_work(api):
    sid = api.session()

    def agent(n, role, convo):
        last = str(convo[-1].get("content"))
        if last.startswith("[harness — not the user] A routine check"):
            return _write("read_path.rs", "// parallel read path\n")
        if "OBSERVATION" in last and "read_path.rs" in last:
            return "FINAL: implemented the read path in read_path.rs."
        return "FINAL: Bottom line: the real work is (a) (b) (c)."
    api.agent = agent
    evs = api.send(sid, ASK5, mode="simple")
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs and msgs[-1] == "implemented the read path in read_path.rs."



# ════════════════════════════════════════════════════════════════════════════
# 6. A message typed during a Team run: directive steers, question/another
#    agent becomes a side task
# ════════════════════════════════════════════════════════════════════════════

DIRECTIVE = ("we can't run python application right ? we have rewritten rust and "
             "go … use our solution")


def _start_team_run_that_blocks(api, sid, content=ASK5):
    """Start a team turn whose stub pipeline blocks mid-run until released; at
    release the Doer's real before_model steer callback runs on a fake request."""
    started, release = threading.Event(), threading.Event()
    seen = {"requests": []}

    def on_run():
        started.set()
        assert release.wait(30), "test never released the team run"
        from aiforge_core.runtime import chat_steer_callback as csc
        cb = csc.make_steer_before_model_callback("doer")

        class Req:
            contents = ["seed"]
        req = Req()
        cb(llm_request=req)
        seen["requests"].append(list(req.contents))
    api.adk["on_run"] = on_run
    api.adk["events"] = [_adk_event("planner", '{"plan": "p"}'),
                         _adk_event("doer", "implemented using the rust and go code")]
    out = {}
    t = threading.Thread(target=lambda: out.update(evs=api.send(sid, content, mode="team")),
                         daemon=True)
    t.start()
    assert started.wait(30), "the team run never started"
    return t, release, out, seen


def test_6_directive_steers_the_team_run_and_reaches_the_doer(api):
    sid = api.session()
    t, release, out, seen = _start_team_run_that_blocks(api, sid)
    r = api.client.post(f"/api/chat/sessions/{sid}/side", json={"content": DIRECTIVE})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "steer" and body.get("queued") is True, body
    assert not api.store.child_sessions(sid)             # no side task was made
    release.set()
    t.join(30)
    assert not t.is_alive()
    folded = " ".join(str(getattr(c, "parts", c)) for req in seen["requests"] for c in req)
    assert "use our solution" in folded, folded           # the Doer's next call saw it
    evs = out["evs"]
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs and "rust and go" in msgs[-1]
    # and the run acknowledged it to the user
    assert any("Got your message" in e.get("text", "") and "use our solution" in e["text"]
               for e in evs if e.get("type") == "thought"), [e.get("type") for e in evs]


@pytest.mark.parametrize("text", [
    "run another agent to check the logs for errors",
    "meanwhile check the logs for errors",
])
def test_6_an_explicit_ask_for_another_agent_becomes_a_side_task(api, monkeypatch, text):
    monkeypatch.setenv("AIFORGE_CHAT_SIDE_TASKS_MAX", "3")
    sid = api.session()
    t, release, out, seen = _start_team_run_that_blocks(api, sid)
    side_agent = {"n": 0}

    def agent(n, role, convo):
        side_agent["n"] += 1
        return "FINAL: the side agent answer: nothing wrong in the logs."
    api.agent = agent
    r = api.client.post(f"/api/chat/sessions/{sid}/side", json={"content": text})
    body = r.json()
    assert body["action"] == "task", body
    child_id = body["task"]["id"]
    assert child_id != sid and api.store.get_session(child_id)["parent_id"] == sid
    # the pending steer queue of the team run did NOT get this text
    from aiforge_core.runtime import chat_interject
    assert text not in chat_interject.peek_texts(sid)
    api.settle(child_id)
    _wait_assistant_turns(api.store, child_id, 1)
    release.set()
    t.join(30)
    assert not t.is_alive()
    # the team turn finished on its own, untouched by the side task
    msgs = [e["text"] for e in out["evs"] if e.get("type") == "message"]
    assert msgs and "implemented using the rust and go code" in msgs[-1]
    assert not any("side agent answer" in m for m in msgs)
    folded = " ".join(str(getattr(c, "parts", c)) for req in seen["requests"] for c in req)
    assert text not in folded
    # the side task's answer is reported back into the parent chat
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        rows = api.store.get_messages(sid)
        if any("Side task:" in str(r_.get("content")) for r_ in rows):
            break
        api.client.get(f"/api/chat/sessions/{sid}/tasks")
        time.sleep(0.1)
    assert any("Side task:" in str(r_.get("content")) and "side agent answer" in str(r_.get("content"))
               for r_ in api.store.get_messages(sid))


def test_6_classification_matrix_by_mode():
    from aiforge_core.api.routes._chat._side_tasks import classify
    assert classify(DIRECTIVE, "team") == "steer"
    assert classify(DIRECTIVE, "simple") == "steer"
    assert classify("use postgres instead", "team") == "steer"
    # a question is not guessed into a side task: the running model reads it
    assert classify("what does the retry helper do?", "team") == "steer"
    assert classify("what does the retry helper do?", "simple") == "steer"
    assert classify("run another agent to check the logs", "team") == "task"
    assert classify("in parallel, count the lines of python", "team") == "task"
    assert classify("stop", "team") == "steer"



# ════════════════════════════════════════════════════════════════════════════
# 7. ONE git worktree per chat: Simple -> Plan -> Team and a 3-subtask build
# ════════════════════════════════════════════════════════════════════════════

def test_7_one_worktree_across_simple_plan_team_and_a_three_subtask_build(api, monkeypatch):
    import re

    from aiforge_core.runtime import parallel_subtasks as pp
    seen_trees = []

    def snap(label):
        seen_trees.append((label, len(trees(api.repo))))

    assert len(trees(api.repo)) == 1                           # the checkout
    sid = api.session()

    # 1. simple
    api.agent = lambda n, role, convo: "FINAL: the cart total sums the list."
    api.send(sid, "what does total() in src/cart.py do?", mode="simple")
    snap("simple")
    wd = api.store.get_session(sid)["workdir"]
    assert wd and os.path.isdir(wd) and os.path.realpath(wd) != os.path.realpath(api.repo)
    assert len(trees(api.repo)) == 2

    # 2. plan (same chat, same worktree)
    api.agent = lambda n, role, convo: "FINAL: 1. add tax\n2. add tests"
    api.send(sid, "plan the tax feature", mode="plan")
    snap("plan")
    assert api.store.get_session(sid)["workdir"] == wd

    # 3. team (sequential ADK pipeline, stubbed stages) - it even names the
    #    project's own path, which must not open a per-run worktree
    api.adk["events"] = [_adk_event("planner", "{}"), _adk_event("doer", "done")]
    api.send(sid, f"fix the total in {api.repo}/src/cart.py", mode="team")
    snap("team")
    assert api.store.get_session(sid)["workdir"] == wd

    # 4. a team BUILD that decomposes into 3 subtasks (real parallel route, one
    #    writer in place by default); every model call is checked for extra worktrees
    files = [{"path": "alpha.py", "purpose": "alpha module", "exports": []},
             {"path": "beta.py", "purpose": "beta module", "exports": []},
             {"path": "gamma.py", "purpose": "gamma module", "exports": []}]
    monkeypatch.setattr(pp, "_enhance", lambda prompt, **k: "SPEC: build alpha, beta, gamma")
    monkeypatch.setattr(pp, "_architect", lambda spec, **k: files)
    during = []
    from aiforge_core.config import approval_settings
    approval_settings.set_mode("team", False)     # else the build runs the gated sequential team

    def writer(n, role, convo):
        during.append(len(trees(api.repo)))
        text = _flat(convo)
        mt = re.findall(r"\b(alpha|beta|gamma)\.py\b", text)
        target = mt[-1] if mt else "alpha"
        if "=== path ===" in text:        # the single-shot subtask runner's protocol
            mt = re.findall(r"\b(alpha|beta|gamma)\.py\b", str(convo[-1].get("content")))
            tgt = mt[0] if mt else target
            return f"=== {tgt}.py ===\n# {tgt}\nVALUE = 1\n"
        if "OBSERVATION" in str(convo[-1].get("content")):
            return f"FINAL: wrote {target}.py"
        return _write(f"{target}.py", f"# {target}\nVALUE = 1\n")
    api.agent = writer
    evs = api.send(sid, "build a new three module package: alpha, beta and gamma modules, "
                        "each in its own file with tests", mode="team")
    snap("build")
    assert any(e.get("type") == "subtasks" for e in evs) or during, \
        "the 3-subtask build did not run: " + str(
            [(e.get("type"), str(e.get("text"))[:200]) for e in evs])
    assert during, "no subtask ran a model call"
    sub_events = [e for e in evs if e.get("type") == "subtasks"]
    # the three modules (+ the test subtasks the planner adds for them)
    assert sub_events and len(sub_events[0]["items"]) >= 3, sub_events
    for name in ("alpha", "beta", "gamma"):
        assert os.path.exists(os.path.join(wd, f"{name}.py")), (
            name, subprocess.run(["find", wd, "-not", "-path", "*/.git/*"], capture_output=True,
                                 text=True).stdout,
            [(r, _flat(c)[:100]) for r, c in api.calls[-12:]])   # built in the chat's worktree
        assert not os.path.exists(os.path.join(api.repo, f"{name}.py"))  # not the user's checkout
    # the count never moved during the build, and no per-subtask branch/worktree is left
    assert set(during) == {2}, during
    assert [n for _l, n in seen_trees] == [2, 2, 2, 2], seen_trees
    assert api.store.get_session(sid)["workdir"] == wd
    leftovers = [b for b in _git(api.repo, "branch", "--format=%(refname:short)").stdout.split()
                 if "-sub-" in b]
    assert not leftovers, leftovers
    assert not os.path.exists(os.path.join(wd, ".aiforge-worktrees"))
    assert len(trees(api.repo)) == 2



# ════════════════════════════════════════════════════════════════════════════
# 8. Task dock / context meter (python side)
# ════════════════════════════════════════════════════════════════════════════

PLAN_JSON = json.dumps({"plan_md": "plan", "subtickets": [
    {"slug": "reader", "goal": "write the reader"},
    {"slug": "bench", "goal": "benchmark it"}]})


def test_8_a_team_turn_emits_usage_events_with_stage_and_per_turn_subtasks(api):
    sid = api.session()
    # turn 1: simple, a multi-part request -> its own subtasks (the parts)
    steps = [_write("alpha.txt", "ALPHA"), _plan("part-1", status="done"),
             _write("beta.txt", "BETA"), _plan("part-2", status="done"),
             _write("gamma.txt", "GAMMA"), _plan("part-3", status="done"),
             _write("delta.txt", "DELTA"), _plan("part-4", status="done"),
             "FINAL: all four created."]
    api.agent = lambda n, role, convo: steps[min(n - 1, len(steps) - 1)]
    evs1 = api.send(sid, BIG_REQUEST, mode="simple")
    assert any(e.get("type") == "usage" and "context_tokens" in e for e in evs1)
    rows = _wait_assistant_turns(api.store, sid, 1)
    sub1 = [s for s in rows[-1]["steps"] if s.get("type") == "subtasks"]
    assert len(sub1) == 1 and len(sub1[0]["items"]) == 4
    assert {i["status"] for i in sub1[0]["items"]} == {"done"}

    # turn 2: team -> usage with stages, planner subtasks of ITS OWN
    api.adk["events"] = [_adk_event("planner", PLAN_JSON),
                         _adk_event("doer", "implemented reader and bench")]
    evs2 = api.send(sid, ASK5, mode="team")
    usage = [e for e in evs2 if e.get("type") == "usage"]
    stages = [e.get("stage") for e in usage]
    assert "team" in stages and "planner" in stages and "doer" in stages, stages
    assert stages.index("planner") < stages.index("doer")
    for e in usage:
        for k in ("context_tokens", "window_tokens", "pct", "compact_at_tokens"):
            assert k in e, (k, e)
    live = [e for e in evs2 if e.get("type") == "subtasks"]
    assert live and [i["slug"] for i in live[0]["items"]] == ["reader", "bench"]
    rows = _wait_assistant_turns(api.store, sid, 2)
    asst = [r for r in rows if r["role"] == "assistant"]
    sub2 = [s for s in (asst[-1].get("steps") or []) if s.get("type") == "subtasks"]
    assert len(sub2) == 1, asst[-1].get("steps")
    assert [i["slug"] for i in sub2[0]["items"]] == ["reader", "bench"]   # not turn 1's parts
    assert not any(i["slug"].startswith("part-") for i in sub2[0]["items"])
    # and turn 1's row still holds its own
    sub1b = [s for s in (asst[0].get("steps") or []) if s.get("type") == "subtasks"]
    assert [i["slug"] for i in sub1b[0]["items"]] == [f"part-{i}" for i in range(1, 5)]
    assert {i["status"] for i in sub2[0]["items"]} == {"done"}            # reconciled to the outcome



# ════════════════════════════════════════════════════════════════════════════
# 9. Persisted handoff: Stop mid-task -> 'continue' resumes; unrelated starts
#    clean; a crash leaves a resumable record
# ════════════════════════════════════════════════════════════════════════════

def _stop_after_two_items(api, sid):
    """Turn 1: works items 1 and 2 of a 4-part request, then the user presses
    Stop (the real /stop endpoint) while the model is working on item 3."""
    n_seen = {"n": 0}

    def agent(n, role, convo):
        n_seen["n"] = n
        script = [_write("alpha.txt", "ALPHA"), _plan("part-1", status="done"),
                  _write("beta.txt", "BETA"), _plan("part-2", status="done")]
        if n <= len(script):
            return script[n - 1]
        if n == len(script) + 1:
            threading.Thread(target=lambda: api.client.post(
                f"/api/chat/sessions/{sid}/stop"), daemon=True).start()
            from aiforge_core.runtime import chat_cancel as cc
            t0 = time.monotonic()
            while not cc.is_cancelled(sid) and time.monotonic() - t0 < 10:
                time.sleep(0.02)
            return _write("gamma.txt", "GAMMA")
        return f"FINAL: {CAP_SENTINEL}"
    api.agent = agent
    evs = api.send(sid, BIG_REQUEST, mode="simple")
    return evs


def test_9_stop_then_continue_resumes_from_the_saved_handoff(api):
    sid = api.session()
    evs = _stop_after_two_items(api, sid)
    assert any(e.get("type") == "error" and "stopped by user" in e.get("text", "")
               for e in evs)
    _wait_assistant_turns(api.store, sid, 1)
    h = api.client.get(f"/api/chat/sessions/{sid}/handoff").json()
    assert h["unfinished"] is True, h
    rec = h["handoff"]
    assert rec["status"] == "stopped"
    assert "alpha.txt" in rec["goal"] and "delta.txt" in rec["goal"]      # the goal
    assert any("part-1" in d or "alpha" in d for d in rec["done"]), rec   # what is done
    assert any("part-3" in o or "gamma" in o for o in rec["open"]), rec   # what is next

    seen_first = {}

    def resume_agent(n, role, convo):
        if "convo" not in seen_first:
            seen_first["convo"] = [dict(m) for m in convo]
        steps = [_write("gamma.txt", "GAMMA"), _plan("part-3", status="done"),
                 _write("delta.txt", "DELTA"), _plan("part-4", status="done"),
                 "FINAL: finished parts 3 and 4."]
        return steps[min(n - 1, len(steps) - 1)]
    # a Stop leaves the unfinished turn: reset the agent for the resumed turn
    from aiforge_core.runtime import chat_cancel
    api.agent = lambda n, role, convo: resume_agent(n, role, convo)
    api.calls.clear()
    evs2 = api.send(sid, "continue", mode="simple")
    flat = _flat(seen_first["convo"])
    assert "HANDOFF" in flat, flat[-1500:]
    assert "delta.txt" in flat                       # the goal is carried
    assert "alpha" in flat.lower() or "part-1" in flat   # what is done
    msgs = [e["text"] for e in evs2 if e.get("type") == "message"]
    assert msgs and "finished parts 3 and 4" in msgs[-1]
    wd = api.store.get_session(sid)["workdir"] or str(api.repo)
    for f in ("alpha.txt", "beta.txt", "gamma.txt", "delta.txt"):
        assert os.path.exists(os.path.join(wd, f)), f
    # finished work leaves nothing to resume
    h2 = api.client.get(f"/api/chat/sessions/{sid}/handoff").json()
    assert not h2["unfinished"], h2


def test_9_an_unrelated_new_request_is_the_turns_request_and_the_model_decides(api):
    """No rule sorts the next message into "continues" or "new task". The
    unfinished work is handed over as REFERENCE under the user's message, and
    the model — which reads the message — decides whether it applies. A turn
    that then leaves the old work alone does not lose it."""
    sid = api.session()
    _stop_after_two_items(api, sid)
    _wait_assistant_turns(api.store, sid, 1)
    assert api.client.get(f"/api/chat/sessions/{sid}/handoff").json()["unfinished"]
    seen_first = {}

    def agent(n, role, convo):
        seen_first.setdefault("convo", [dict(m) for m in convo])
        return "FINAL: there is no tax_rate helper in billing; nothing to rename."
    api.agent = agent
    ask = "rename the helper tax_rate to vat_rate in billing"
    evs = api.send(sid, ask, mode="simple")
    last_user = next(str(m.get("content")) for m in reversed(seen_first["convo"])
                     if m.get("role") == "user")
    assert last_user.startswith(ask)                       # the user's words lead
    assert "[HANDOFF — for reference only." in last_user and "[RESUME" not in last_user
    assert "The user's new message, above, is the request for this turn" in last_user
    assert "do not resume the earlier work on your own" in last_user
    assert "ORIGINAL REQUEST" not in last_user             # the goal is the new message
    msgs = [e["text"] for e in evs if e.get("type") == "message"]
    assert msgs and "nothing to rename" in msgs[-1]
    # the earlier work was not touched and is still there for a later "continue"
    h = api.client.get(f"/api/chat/sessions/{sid}/handoff").json()
    assert h["unfinished"], h


def test_9_a_crash_mid_run_leaves_a_resumable_record(api):
    """The run's generator is closed (the process/stream dies) in the middle
    of work: the chat still has a record the next 'continue' resumes from."""
    sid = api.session()
    script = [_write("alpha.txt", "ALPHA"), _plan("part-1", status="done"),
              _write("beta.txt", "BETA")]
    seen = []

    def fn(role, convo):
        seen.append(1)
        return script[min(len(seen), len(script)) - 1]
    gen = ca.run_chat_agent([{"role": "user", "content": BIG_REQUEST}],
                            cwd=str(api.repo), complete_fn=fn, session_id=sid)
    n_tools = 0
    for ev in gen:
        if ev.get("type") == "tool" and ev.get("name") == "file_write":
            n_tools += 1
            if n_tools == 2:
                break                       # the consumer vanishes mid-turn
    gen.close()
    from aiforge_core.runtime import handoff_store
    rec = handoff_store.load(sid)
    assert rec is not None and handoff_store.is_unfinished(rec), rec
    assert rec["status"] in ("interrupted", "stopped") and "alpha.txt" in rec["goal"]
    # the next message continues from it
    rows = [{"role": "user", "content": BIG_REQUEST, "mode": "simple"},
            {"role": "user", "content": "continue", "mode": "simple"}]
    hist = [{"role": "user", "content": BIG_REQUEST}, {"role": "user", "content": "continue"}]
    text = handoff_store.seed_next_turn(sid, rows, "continue", hist)
    assert "alpha.txt" in text and "HANDOFF" in text.upper()
    assert "HANDOFF" in hist[-1]["content"].upper()      # the history was rewritten to carry it



# ════════════════════════════════════════════════════════════════════════════
# 10. The ticket/team PIPELINE: stalled Doer loop -> re-plan with the failed-
#     approaches list; model-stage failures retry until cancelled
# ════════════════════════════════════════════════════════════════════════════

def _req_text(llm_request) -> str:
    parts = []
    try:
        parts.append(str(llm_request.config.system_instruction or ""))
    except Exception:  # noqa: BLE001
        pass
    for c in getattr(llm_request, "contents", None) or []:
        for p in getattr(c, "parts", None) or []:
            parts.append(str(getattr(p, "text", "") or ""))
    return "\n".join(parts)


class Pipe:
    """The real build_pipeline graph with stub BaseLlm stages and the real text
    Doer (its model calls go through the scripted llm.client seam)."""

    def __init__(self, monkeypatch, tmp_path):
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_response import LlmResponse
        from google.genai import types as gt
        self.calls, self.planner_requests = [], []
        self.doer_seeds = []             # the seed (first user turn) of each Doer pass
        self.replies = {
            "triage": '{"complexity": "moderate", "estimated_files": 3, "rationale": "x"}',
            "feedback": "fail\nthe change is not working yet",
            "validator": '{"verdict": "approve", "rationale": "ok", "scope_ok": true, '
                         '"tests_present": true, "regression_risk": "low"}',
            "planner": 'PLAN: {"subtickets": [{"slug": "s1", "goal": "fix it"}]}',
            "verifier": '{"verdict": "pass", "rationale": "ok"}',
        }
        self.fail_for = {}               # role -> callable(n_call) raising or None
        me = self

        def make_stub(role):
            class _Stub(BaseLlm):
                async def generate_content_async(self, llm_request, stream=False):
                    me.calls.append(role)
                    if role == "planner":
                        me.planner_requests.append(_req_text(llm_request))
                    hook = me.fail_for.get(role)
                    if hook:
                        hook(sum(1 for r in me.calls if r == role))
                    yield LlmResponse(content=gt.Content(role="model", parts=[
                        gt.Part(text=me.replies.get(role, f"{role} output"))]))
            return _Stub(model="stub")
        self.make_stub = make_stub
        monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
        monkeypatch.setenv("AIFORGE_OBSERVABILITY_DISABLE", "1")
        monkeypatch.setenv("AIFORGE_DOER_PROTOCOL", "text")
        monkeypatch.setenv("AIFORGE_CHAT_TOOL_PROTOCOL", "text")
        monkeypatch.setenv("AIFORGE_DOER_MIN_EDIT_RETRIES", "0")
        import aiforge_core.runtime.pipeline as pl
        self.pl = pl
        monkeypatch.setattr(pl, "build_litellm_model", lambda role: self.make_stub(role))
        # the REAL production default (conftest pins it to 0 for the old tests)
        from aiforge_core.runtime.graph_pipeline import _gates
        monkeypatch.setattr(_gates, "PLATEAU_REPLANS", 2)
        assert _gates.NO_EDIT_ITERS == 2
        self.gates = _gates
        # the Doer's own model calls
        self.repo = tmp_path / "pipe-repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hello\n")
        (self.repo / "test_bad.py").write_text("def test_x():\n    assert 1 == 2\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        monkeypatch.setenv("AIFORGE_REPO_ROOT", str(self.repo))
        self.doer_script = lambda n, convo: "FINAL: looks fine"
        self.doer_n = 0
        from aiforge_core.llm import client as llm_client

        def complete(role, messages, **kw):
            if role not in ("doer", "chat"):
                return "FINAL: n/a"
            if len(messages) <= 2:           # first step of a Doer pass: record its seed
                self.doer_seeds.append(str(messages[1].get("content")))
            self.doer_n += 1
            return self.doer_script(self.doer_n, messages)
        monkeypatch.setattr(llm_client, "_complete_impl", complete)

    def drive(self, prompt="# Ticket T-1\nfix the thing", timeout=150):
        import asyncio

        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types as gt
        wf = self.pl.build_pipeline(skip_researcher=True)

        async def _go():
            svc = InMemorySessionService()
            runner = Runner(agent=wf, app_name="t", session_service=svc,
                            auto_create_session=True)
            session = await svc.create_session(app_name="t", user_id="u")
            msg = gt.Content(role="user", parts=[gt.Part(text=prompt)])
            async for _ in runner.run_async(user_id="u", session_id=session.id,
                                            new_message=msg):
                pass
            s = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
            return dict(s.state or {})
        return asyncio.run(asyncio.wait_for(_go(), timeout=timeout))


@pytest.fixture
def pipe(monkeypatch, tmp_path):
    return Pipe(monkeypatch, tmp_path)


def test_10_two_no_edit_passes_replan_with_the_failed_approaches_in_the_doer_seed(pipe):
    pipe.doer_script = lambda n, convo: (
        'ACTION: file_read\nARGS_JSON: {"path": "README.md"}'
        if "OBSERVATION" not in str(convo[-1].get("content")) else "FINAL: it already looks fine")
    state = pipe.drive()
    assert state.get("plateau_replan_count") == 2, state.get("plateau_replan_count")
    assert state.get("_no_replan_reason") == "doer_plateau"       # bounded: then ships
    assert pipe.calls.count("planner") == 3                        # the plan + 2 re-plans
    # the re-planned Planner was told the last approach stalled
    assert "DIFFERENT approach" in pipe.planner_requests[1]
    # Doer seeds: the first has no failed list; after the stall every seed has it
    assert "APPROACHES THAT ALREADY FAILED" not in pipe.doer_seeds[0]
    later = [s for s in pipe.doer_seeds[2:]]
    assert later and all("APPROACHES THAT ALREADY FAILED" in s for s in later)
    assert any("a pass that changed no file" in s for s in later)
    assert any("the previous plan stalled" in s for s in later)
    assert any("REPLAN NOTE" in s for s in later)
    # no edit was ever made, and the run is bounded (2 passes per attempt, 3 attempts)
    assert len(pipe.doer_seeds) == 6, len(pipe.doer_seeds)


def test_10_the_same_test_failure_survives_fix_after_fix_and_is_replanned(pipe):
    import sys
    cmd = f"{sys.executable} -m pytest -q test_bad.py"

    def doer(n, convo):
        last = str(convo[-1].get("content"))
        calls = sum(1 for m in convo if m.get("role") == "assistant")
        if calls == 0:           # a different edit every pass (so the no-edit rule is not it)
            return ("ACTION: file_write\nARGS_JSON: "
                    + json.dumps({"path": f"fix_{n}.txt", "content": f"attempt {n}\n"}))
        if calls == 1:
            return "ACTION: run_command\nARGS_JSON: " + json.dumps({"cmd": cmd})
        return "FINAL: tried another fix"
    pipe.doer_script = doer
    state = pipe.drive(timeout=170)
    seeds = pipe.doer_seeds
    assert state.get("plateau_replan_count") == 2
    assert state.get("_no_replan_reason") == "doer_plateau"        # bounded, then it ships
    # the failed list is in the Doer seed from the second pass on, and names the failure
    assert "APPROACHES THAT ALREADY FAILED" not in seeds[0]
    assert all("APPROACHES THAT ALREADY FAILED" in s for s in seeds[1:])
    assert any("the tests failed" in s for s in seeds[1:])
    # the same failure earned a directed nudge (the 3rd sighting) before the stop
    assert any("SAME failure survived" in s for s in seeds)
    # after the stop the planner was asked for a DIFFERENT approach
    assert pipe.calls.count("planner") == 3
    assert any("DIFFERENT approach" in r for r in pipe.planner_requests[1:])
    assert any("the previous plan stalled" in s for s in seeds[1:])
    assert len(seeds) < 40, len(seeds)               # bounded: it ships, it does not run on


def test_10_a_model_stage_that_keeps_failing_is_retried_until_it_answers(pipe, monkeypatch):
    from aiforge_core.runtime.escalating_llm import _wrapper as W
    monkeypatch.delenv("AIFORGE_PIPELINE_PERSIST_S", raising=False)      # production: 0 = until cancelled
    orig_gap = W.EscalatingLlm._persist_gap
    monkeypatch.setattr(W.EscalatingLlm, "_persist_gap",
                        lambda self, exc, rounds, t0: (None if orig_gap(self, exc, rounds, t0) is None
                                                       else 0.01))
    real_sleep = W.asyncio.sleep

    async def quick(delay, *a, **k):
        return await real_sleep(0)
    monkeypatch.setattr(W.asyncio, "sleep", quick)
    failures = {"n": 0}

    def planner_down(n):
        if failures["n"] < 7:
            failures["n"] += 1
            raise RuntimeError("HTTP 500 internal server error")
    pipe.fail_for["planner"] = planner_down
    pipe.replies["feedback"] = "pass\nall good"
    pipe.doer_script = lambda n, convo: (
        "FINAL: done" if "OBSERVATION" in str(convo[-1].get("content"))
        else 'ACTION: file_write\nARGS_JSON: {"path": "fix.txt", "content": "fixed\\n"}')
    pl = pipe.pl
    stub_for = pipe.make_stub

    def build(role):
        if role == "planner":
            return W.EscalatingLlm(model="stub", role=role, primary_model=stub_for(role),
                                   chain_models=[], chain_labels=[])
        return stub_for(role)
    monkeypatch.setattr(pl, "build_litellm_model", build)
    state = pipe.drive(timeout=120)
    assert failures["n"] == 7                        # it really failed seven times first
    assert str(state.get("feedback_verdict", "")).startswith("pass")
    assert state.get("plan_md")                      # the stage finally answered
    assert (pipe.repo / "fix.txt").exists()          # and the work went on to the Doer


def test_10_pipeline_persist_is_bounded_only_when_asked(pipe, monkeypatch):
    """Default = until cancelled: an always-failing stage is still being retried
    when the run is cancelled (it never raises); the old give-up is -1."""
    import asyncio

    from aiforge_core.runtime.escalating_llm import _wrapper as W
    monkeypatch.delenv("AIFORGE_PIPELINE_PERSIST_S", raising=False)
    orig_gap = W.EscalatingLlm._persist_gap
    monkeypatch.setattr(W.EscalatingLlm, "_persist_gap",
                        lambda self, exc, rounds, t0: (None if orig_gap(self, exc, rounds, t0) is None
                                                       else 0.01))
    real_sleep = W.asyncio.sleep

    async def quick(delay, *a, **k):
        return await real_sleep(0.005)
    monkeypatch.setattr(W.asyncio, "sleep", quick)

    def always_down(n):
        raise RuntimeError("HTTP 500 internal server error")
    pipe.fail_for["planner"] = always_down
    stub_for = pipe.make_stub
    monkeypatch.setattr(pipe.pl, "build_litellm_model", lambda role: (
        W.EscalatingLlm(model="stub", role=role, primary_model=stub_for(role),
                        chain_models=[], chain_labels=[]) if role == "planner"
        else stub_for(role)))
    with pytest.raises(asyncio.TimeoutError):          # still retrying when cancelled
        pipe.drive(timeout=6)
    assert pipe.calls.count("planner") > 10
    # the old behaviour is one setting away
    monkeypatch.setenv("AIFORGE_PIPELINE_PERSIST_S", "-1")
    pipe.calls.clear()
    with pytest.raises(Exception) as ei:
        pipe.drive(timeout=60)
    assert not isinstance(ei.value, asyncio.TimeoutError)



# ── regressions found while writing the scenarios above ─────────────────────

def test_run_tooling_in_the_tree_is_not_the_requests_work(tmp_path):
    """A team run that edited nothing but whose code-graph index appeared in the
    tree used to show a 'Changes' card (so the run counted as implemented)."""
    from aiforge_core.runtime import chat_pipeline_events as ev
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".codegraph").mkdir()
    (repo / ".codegraph" / ".gitignore").write_text("*\n!.gitignore\n")
    assert ev._team_change_events(str(repo), sha, None) == []
    (repo / "a.py").write_text("x = 2\n")                       # a real edit still shows
    got = ev._team_change_events(str(repo), sha, None)
    assert got and [f["path"] for f in got[0]["files"]] == ["a.py"]


def test_an_installed_venv_in_the_tree_is_not_test_gaming(tmp_path):
    """pytest's own source (inside a managed .aiforge-venv) assigns into pytest;
    scanned as the model's production code it ended a green run as 'gaming'."""
    from aiforge_core.runtime import gaming_check
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    vend = repo / ".aiforge-venv" / "lib" / "python3.12" / "site-packages" / "_pytest"
    vend.mkdir(parents=True)
    (vend / "skipping.py").write_text(
        'import pytest\n\ndef f(config, old):\n'
        '    config.add_cleanup(lambda: setattr(pytest, "xfail", old))\n')
    assert gaming_check.check(str(repo)) == []
    (repo / "mine.py").write_text(
        'import pytest\n\ndef f(old):\n    setattr(pytest, "xfail", old)\n')
    assert gaming_check.check(str(repo)), "the model's own gaming code is still found"


# --- END ---
