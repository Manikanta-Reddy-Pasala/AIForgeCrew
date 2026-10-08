"""One rolling context: the pieces that keep a long task in one context and
the prompt cache warm — context mode, prune at the condense point, the
stable system prompt, diffs on a re-read, the transcript across messages,
and the outline a split subtask gets."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_context_mode, chat_transcript, context_seen
from aiforge_core.runtime.chat_agent._context import _aging


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for k in ("AIFORGE_CHAT_CONTEXT", "AIFORGE_CHAT_CARRY_TRANSCRIPT", "AIFORGE_CHAT_AGE_OBS",
              "AIFORGE_CHAT_ITEM_CONTEXT_RESET"):
        monkeypatch.delenv(k, raising=False)


# ── context mode ─────────────────────────────────────────────────────────────

def test_context_mode_defaults_to_one(monkeypatch):
    assert chat_context_mode.resolve(None) == "one"
    assert chat_context_mode.resolve("split") == "split"
    assert chat_context_mode.resolve("weird") == "one"
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT", "split")
    assert chat_context_mode.resolve(None) == "split"
    assert chat_context_mode.resolve("one") == "one"         # the message wins


def test_one_context_never_auto_splits_a_build(monkeypatch):
    from aiforge_core.api.routes._chat import _routing
    from aiforge_core.runtime import chat_router
    seen = {}

    def fake_decide(prompt, **kw):
        seen.update(kw)
        return kw
    monkeypatch.setattr(chat_router, "decide", fake_decide)
    pp = SimpleNamespace(enabled=lambda: True)
    _routing._decide_chat_route(pp, "build a full app with 5 modules", "act", False, False,
                                "/tmp", [], context="one")
    assert seen["auto_escalate"] is False
    _routing._decide_chat_route(pp, "build a full app with 5 modules", "act", False, False,
                                "/tmp", [], context="split")
    assert seen["auto_escalate"] is True


def test_item_reset_and_step_ageing_are_off_by_default():
    from aiforge_core.runtime.chat_agent._turn import _items
    assert _items.reset_enabled() is False
    assert _aging._on() is False


# ── prune at the condense point ──────────────────────────────────────────────

def _obs_convo(n_reads=8, size=6000):
    convo = [{"role": "system", "content": "sys"}]
    for i in range(n_reads):
        convo.append({"role": "assistant",
                      "content": f'ACTION: file_read\nARGS_JSON: {{"path": "f{i}.py"}}'})
        convo.append({"role": "user", "content": "OBSERVATION: " + ("x" * size)})
    return convo


def test_forced_prune_keeps_the_newest_whole_and_shrinks_the_rest():
    convo = _obs_convo()
    before = sum(len(m["content"]) for m in convo)
    forget = {f'file_read|{{"path": "f{i}.py"}}' for i in range(8)}
    n = _aging.age_observations(convo, force=True, keep_chars=13_000, forget=forget)
    after = sum(len(m["content"]) for m in convo)
    assert n >= 5 and after < before / 2
    assert "[aged:" not in convo[-1]["content"]              # newest stays whole
    assert "[aged:" in convo[2]["content"]
    assert 'file_read|{"path": "f0.py"}' not in forget        # may be read again


def test_prune_keeps_less_on_a_small_window(monkeypatch):
    """On a small window a fixed 25K tokens kept whole would leave nothing to
    prune: the keep is capped at 40% of the budget."""
    from aiforge_core.runtime.chat_agent._turn import _limits
    from aiforge_core.runtime.chat_agent._context import _window
    st = SimpleNamespace(convo=_obs_convo(12), batch_unread=False, batch_mark=0, read_sigs_seen=set())
    monkeypatch.setattr(_window, "_ctx_budget_chars", lambda *a, **k: 40_000)
    assert _limits._prune_at_condense_point(st, "doer") > 0


def test_prune_stage_runs_only_over_budget(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _limits
    from aiforge_core.runtime.chat_agent._context import _window
    # 40 reads × 6000 chars: well past the ~100K chars of newest history kept whole.
    st = SimpleNamespace(convo=_obs_convo(40), batch_unread=False, batch_mark=0, read_sigs_seen=set())
    monkeypatch.setattr(_window, "_ctx_budget_chars", lambda *a, **k: 10_000_000)
    assert _limits._prune_at_condense_point(st, "doer") == 0
    monkeypatch.setattr(_window, "_ctx_budget_chars", lambda *a, **k: 150_000)
    assert _limits._prune_at_condense_point(st, "doer") > 0


# ── stable system prompt ─────────────────────────────────────────────────────

def test_stable_system_prompt_moves_message_blocks_to_a_note(tmp_path):
    from aiforge_core.runtime.chat_agent._turn._convo import _build_convo, is_turn_note
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")

    def build(text, stable):
        msgs = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "ok"},
                {"role": "user", "content": text}]
        convo, *_ = _build_convo(msgs, str(tmp_path), "chat", readonly_mode=False,
                                 plan_mode=False, analyze_mode=False, builder=None,
                                 strict_finish=False, session_id=None, native=True,
                                 stable_system=stable)
        return convo
    a = build("1. add a test\n2. rename f to g\n3. update the docs", True)
    b = build("explain f", True)
    assert a[0]["content"] == b[0]["content"]                 # byte-identical system
    assert any(is_turn_note(m) for m in a)                    # the checklist moved here
    assert "MULTI-PART REQUEST" not in a[0]["content"]
    assert a[-1]["content"].startswith("1. add a test")       # the user's message untouched
    old = build("1. add a test\n2. rename f to g\n3. update the docs", False)
    assert "MULTI-PART REQUEST" in old[0]["content"]          # off: as before


def test_transcript_drops_turn_notes(monkeypatch, tmp_path):
    from aiforge_core.runtime.chat_agent._turn._convo import TURN_NOTE_ACK, TURN_NOTE_OPEN
    import json
    asked = [{"role": "user", "content": "q"}]
    convo = [{"role": "system", "content": "s"},
             {"role": "user", "content": TURN_NOTE_OPEN + "\nrules"},
             {"role": "assistant", "content": TURN_NOTE_ACK},
             asked[0], {"role": "user", "content": "OBSERVATION: x"}]
    assert chat_transcript.save(5, asked, convo, answer="done")
    data = json.loads((tmp_path / "chat_transcripts" / "session_5.json").read_text())
    assert not any(TURN_NOTE_OPEN in m["content"] or m["content"] == TURN_NOTE_ACK
                   for m in data["messages"])


def test_transcript_matches_the_users_words_not_the_harness_additions():
    asked = [{"role": "user", "content": "fix calc.py\n\n---\n[Interpreted request — x]\nrestated"}]
    convo = [{"role": "system", "content": "s"}, asked[0],
             {"role": "user", "content": "OBSERVATION: read"}]
    assert chat_transcript.save(9, asked, convo, answer="Fixed.")
    nxt = [{"role": "user", "content": "fix calc.py"},          # as stored in the chat
           {"role": "assistant", "content": "Fixed."},
           {"role": "user", "content": "now the tests"}]
    got = chat_transcript.carried(9, nxt)
    assert got is not None and got[-1]["content"] == "now the tests"


# ── diff on a re-read ────────────────────────────────────────────────────────

def test_reread_after_a_change_gets_only_the_diff():
    context_seen.reset_seen_bodies()
    old = "\n".join(f"line {i}" for i in range(400))
    new = old.replace("line 200", "line 200 changed")
    first = context_seen.dedupe_tool_result([], "file_read", {"path": "a.py"},
                                            {"ok": True, "content": old})
    assert first["content"] == old
    msgs = [{"role": "user", "content": "OBSERVATION: " + old}]
    second = context_seen.dedupe_tool_result(msgs, "file_read", {"path": "a.py"},
                                             {"ok": True, "content": new})
    assert second.get("changed_only") and "+line 200 changed" in second["content"]
    assert len(second["content"]) < len(new) / 4


def test_reread_is_whole_when_the_old_text_left_the_context():
    context_seen.reset_seen_bodies()
    old = "\n".join(f"line {i}" for i in range(400))
    context_seen.dedupe_tool_result([], "file_read", {"path": "a.py"}, {"ok": True, "content": old})
    new = old.replace("line 5", "line five")
    got = context_seen.dedupe_tool_result([{"role": "user", "content": "[aged: …]"}],
                                          "file_read", {"path": "a.py"}, {"ok": True, "content": new})
    assert got["content"] == new and not got.get("changed_only")


# ── split subtasks see what exists ───────────────────────────────────────────

def test_split_subtask_gets_an_outline_of_existing_files(tmp_path):
    from aiforge_core.runtime.parallel_subtasks._runners import _doer_message
    pkg = tmp_path / "shopkit"
    pkg.mkdir()
    (pkg / "models.py").write_text('ZERO = 0\n\n\nclass Product:\n    pass\n\n\ndef money(x):\n    return x\n')
    msg = _doer_message({"scope_allowlist_globs": ["shopkit/*.py"]}, "", "shopkit/coupons.py",
                        "add coupons", str(tmp_path))
    assert "EXISTING CODE" in msg and "class Product" in msg and "ZERO = 0" in msg
    assert "def money" in msg
    bare = _doer_message({}, "", "", "add coupons", str(tmp_path))
    assert "EXISTING CODE" not in bare


# ── fresh context endpoint ───────────────────────────────────────────────────

def test_fresh_context_drops_the_transcript(monkeypatch):
    from aiforge_core.api.routes._chat import _sessions
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_session", lambda sid: {"id": sid})
    asked = [{"role": "user", "content": "q"}]
    chat_transcript.save(3, asked, [{"role": "system", "content": "s"}, asked[0]], answer="a")
    assert os.path.exists(chat_transcript._path(3))
    assert _sessions.chat_session_fresh_context(3) == {"ok": True}
    assert not os.path.exists(chat_transcript._path(3))


# ── review cases ─────────────────────────────────────────────────────────────

def test_only_the_chat_routes_turn_saves_a_transcript(monkeypatch, tmp_path):
    """A pipeline Doer or a scheduled job runs with the chat's session id; it
    must not overwrite the chat's transcript."""
    from aiforge_core.runtime.chat_agent import _loop

    class _St:
        complete_fn = None
        convo = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]

    def fake_inner(st, *a, **k):
        yield {"type": "message", "text": "done"}
    monkeypatch.setattr(_loop, "_build_loop_state", lambda *a, **k: _St())
    monkeypatch.setattr(_loop, "_drive", fake_inner)
    msgs = [{"role": "user", "content": "go"}]
    list(_loop.run_chat_agent(msgs, cwd=str(tmp_path), session_id=11))
    assert not os.path.exists(chat_transcript._path(11))
    list(_loop.run_chat_agent(msgs, cwd=str(tmp_path), session_id=11, carry=True))
    assert os.path.exists(chat_transcript._path(11))


def test_a_later_answer_clears_an_earlier_error(monkeypatch, tmp_path):
    from aiforge_core.runtime.chat_agent import _loop

    class _St:
        complete_fn = None
        convo = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]

    def fake_inner(st, *a, **k):
        yield {"type": "error", "text": "a tool failed"}
        yield {"type": "message", "text": "Recovered and done."}
    monkeypatch.setattr(_loop, "_build_loop_state", lambda *a, **k: _St())
    monkeypatch.setattr(_loop, "_drive", fake_inner)
    list(_loop.run_chat_agent([{"role": "user", "content": "go"}], cwd=str(tmp_path),
                              session_id=12, carry=True))
    assert os.path.exists(chat_transcript._path(12))


def test_transcript_never_opens_on_an_answer():
    import json
    asked = [{"role": "user", "content": "q"}]
    convo = [{"role": "system", "content": "s"},
             {"role": "assistant", "content": "ACTION: run_command\nARGS_JSON: {}"},
             {"role": "user", "content": "OBSERVATION: ok"}]
    assert chat_transcript.save(13, asked, convo, answer="done")
    data = json.loads(open(chat_transcript._path(13)).read())
    assert data["messages"][0]["role"] == "user"


def test_ratio_is_kept_per_chat_and_seeded_from_the_transcript():
    from aiforge_core.llm import ctx_ratio
    ctx_ratio.reset()
    msgs = [{"role": "user", "content": "x" * 30_000}]
    tok = ctx_ratio.bind(1)
    ctx_ratio.note("doer", msgs, 10_000)
    ctx_ratio.unbind(tok)
    tok = ctx_ratio.bind(2)
    assert ctx_ratio.chars_per_token("doer") is None           # another chat's measure
    ctx_ratio.seed("doer", 3.2)
    assert ctx_ratio.chars_per_token("doer") == pytest.approx(3.2)
    ctx_ratio.note("doer", msgs, 12_000)                       # its own measure wins
    assert ctx_ratio.chars_per_token("doer") == pytest.approx(2.5)
    ctx_ratio.unbind(tok)
    ctx_ratio.reset()


def test_turn_note_is_pinned_back_after_a_condense():
    from aiforge_core.runtime.chat_agent._context import _note
    from aiforge_core.runtime.chat_agent._turn import _limits
    st = SimpleNamespace(turn_note_text="<<AIFORGE_TURN_CONTEXT>>\nRULE: use Decimal",
                         convo=[{"role": "system", "content": "s"}, _note.build("goal"), _note.ack(),
                                {"role": "user", "content": "OBSERVATION: x"}])
    _limits._repin_turn_note(st)
    assert "RULE: use Decimal" in st.convo[1]["content"]
    _limits._repin_turn_note(st)                               # not twice
    assert st.convo[1]["content"].count("RULE: use Decimal") == 1


def test_turn_note_after_a_user_turn_goes_at_the_end_and_is_cut_on_save():
    from aiforge_core.runtime.chat_agent._turn._convo import _insert_turn_note
    import json
    convo = [{"role": "system", "content": "s"}, {"role": "user", "content": "first"},
             {"role": "user", "content": "my words"}]
    _insert_turn_note(convo, "rules here")
    assert convo[-1]["content"].startswith("my words")
    asked = [{"role": "user", "content": "first"}, {"role": "user", "content": "my words"}]
    assert chat_transcript.save(14, asked, convo, answer="ok")
    data = json.loads(open(chat_transcript._path(14)).read())
    assert not any("rules here" in m["content"] for m in data["messages"])
    assert any("my words" in m["content"] for m in data["messages"])


def test_playbooks_need_a_real_match():
    from aiforge_core.runtime import skills
    pool = [skills.Skill(name="review-pull-request", description="How to review a pull request: fetch the diff, comment",
                         body="steps", triggers=(), source="x", priority=0, always=False)]
    coding = skills.search("Add coupon codes to shopkit and add tests to orders.py", None, skills=pool)
    review = skills.search("review this pull request please", None, skills=pool)
    assert not coding or coding[0]["score"] < skills._inject_min()
    assert review and review[0]["score"] >= skills._inject_min()


# ── context limits: learn from a refusal, stop a shrink loop ─────────────────

@pytest.mark.parametrize("text,window,asked", [
    ("This model's maximum context length is 131072 tokens. However, you requested 140000 tokens", 131072, 140000),
    ("the request exceeds the available context size, try increasing it (n_ctx: 125000)", 125000, 0),
    ("maximum context length is 131,072 tokens, you requested 140,000 tokens", 131072, 140000),
    ("max input length 4096 exceeded", 0, 0),
    ("Prompt has 130512 tokens which exceeds context length of 125000", 125000, 130512),
    ("context_length_exceeded", 0, 0),
])
def test_overflow_refusal_teaches_window_and_ratio(text, window, asked):
    from aiforge_core.llm import ctx_ratio
    ctx_ratio.reset()
    tok = ctx_ratio.bind(21)
    msgs = [{"role": "user", "content": "x" * 400_000}]
    learned = ctx_ratio.learn_from_overflow("doer", msgs, text)
    assert ctx_ratio.learned_window("doer") == window
    tokens = asked or (int(window * 1.1) if window else 0)
    if tokens:
        assert learned and ctx_ratio.chars_per_token("doer") == pytest.approx(400_000 / tokens)
    else:
        assert not learned
    ctx_ratio.unbind(tok)
    ctx_ratio.reset()


def test_a_smaller_server_window_wins_over_the_setting(monkeypatch):
    from aiforge_core.llm import ctx_ratio
    from aiforge_core.runtime.chat_agent._context import _window
    ctx_ratio.reset()
    monkeypatch.setattr(_window, "_window_tokens", lambda role=None: 262_144)
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    big = _window._ctx_budget_chars("doer", sys_chars=0)
    ctx_ratio.learn_from_overflow("doer", [{"role": "user", "content": "x" * 390_000}],
                                  "maximum context length is 125000 tokens. However, you requested 130000 tokens")
    small = _window._ctx_budget_chars("doer", sys_chars=0)
    assert small < big / 2                                     # 75% of 125K at 3 chars/token
    ctx_ratio.reset()


def test_first_overflow_resizes_instead_of_resending(monkeypatch):
    """A "too long" refusal: the history is resized before the next send, so
    the identical oversized prompt is not sent a second time."""
    from aiforge_core.llm import retry_policy as rp

    class Over(Exception):
        pass
    order: list = []
    monkeypatch.setattr(rp, "is_overflow", lambda e: isinstance(e, Over))
    monkeypatch.setattr(rp, "llm_issue", lambda e: None)
    monkeypatch.setattr(rp, "sweep_plan", lambda policy, exc, sc: (3, 0, False, 0))
    hooks = rp.Hooks(call=lambda: order.append("send") or "answer", cancelled=lambda: False,
                     pause=lambda *a, **k: None, shrink=lambda: False, on_overflow=lambda: "",
                     stopped=object(),
                     on_first_overflow=lambda exc: order.append("resize") or "shrank it")
    policy = rp.RetryPolicy.from_env()
    gen = rp.run_with_policy(hooks, policy, Over("maximum context length is 1000"))
    statuses = []
    try:
        while True:
            statuses.append(next(gen))
    except StopIteration:
        pass
    assert order[:2] == ["resize", "send"]
    assert any("over the model's context window" in str(s) for s in statuses)


def test_shrinking_again_and_again_tightens_then_stops():
    from aiforge_core.runtime.chat_agent._turn import _limits
    st = SimpleNamespace(convo=[{"role": "system", "content": "s"},
                                {"role": "user", "content": "OBSERVATION: x"}])
    for n in (1, 3):
        st.step_n = n
        assert _limits._thrash_guard(st) == []
    st.step_n = 5
    ev = _limits._thrash_guard(st)                              # 3 within 8 steps
    assert st.ctx_tight and ev and "read files in ranges" in st.convo[-1]["content"]
    for n in (6, 7):
        st.step_n = n
        _limits._thrash_guard(st)
    assert getattr(st, "ctx_stop", False)                       # 5 within 10 steps


def test_spread_out_shrinks_do_not_trip_the_guard():
    from aiforge_core.runtime.chat_agent._turn import _limits
    st = SimpleNamespace(convo=[{"role": "system", "content": "s"}])
    for n in (5, 20, 35, 50, 65, 80):
        st.step_n = n
        _limits._thrash_guard(st)
    assert not getattr(st, "ctx_tight", False) and not getattr(st, "ctx_stop", False)


def test_carried_condense_note_drops_the_old_goal_and_old_rules():
    import json
    from aiforge_core.runtime.chat_agent._context import _note
    from aiforge_core.runtime.chat_agent._context._compaction import _GOAL_PIN_CLOSE, _GOAL_PIN_OPEN
    note = _note.build(f"{_GOAL_PIN_OPEN}\nORIGINAL TASK: fix X\n{_GOAL_PIN_CLOSE}",
                       "SUMMARY: edited a.py")
    note["content"] += "\n\n<<AIFORGE_TURN_CONTEXT>>\nRULE: old checklist"
    asked = [{"role": "user", "content": "fix X"}]
    convo = [{"role": "system", "content": "s"}, note, _note.ack(), asked[0],
             {"role": "user", "content": "OBSERVATION: y"}]
    assert chat_transcript.save(22, asked, convo, answer="done")
    text = json.dumps(json.loads(open(chat_transcript._path(22)).read())["messages"])
    assert "SUMMARY: edited a.py" in text
    assert "ORIGINAL TASK: fix X" not in text and "old checklist" not in text
    assert "continue the task from here" not in text


def test_posts_after_the_turn_reach_the_next_message():
    """A background command that finished after the answer is posted to the
    chat; the carried transcript must not hide it from the agent."""
    asked = [{"role": "user", "content": "rebuild it"}]
    convo = [{"role": "system", "content": "s"}, asked[0],
             {"role": "user", "content": "OBSERVATION: started bg-1"},
             {"role": "assistant", "content": "Started the build (bg-1)."}]
    assert chat_transcript.save(31, asked, convo, answer="Started the build (bg-1).")
    merged = ("Started the build (bg-1).\n\nBackground command finished (exit 1): scp — "
              "cp: target '/tmp/buildsrc/': No such file or directory")
    nxt = [asked[0], {"role": "assistant", "content": merged}, {"role": "user", "content": "fix it"}]
    got = chat_transcript.carried(31, nxt)
    assert got is not None and "No such file or directory" in got[-2]["content"]
    assert any("OBSERVATION: started bg-1" in m["content"] for m in got)   # the work stays


def test_only_the_posts_after_the_answer_are_added():
    asked = [{"role": "user", "content": "go"}]
    convo = [{"role": "system", "content": "s"}, asked[0], {"role": "user", "content": "OBSERVATION: x"}]
    chat_transcript.save(32, asked, convo, answer="Answer text.")
    same = [asked[0], {"role": "assistant", "content": "Answer text."}, {"role": "user", "content": "next"}]
    assert chat_transcript.carried(32, same)[-2]["content"] == "Answer text."
    other = [asked[0], {"role": "assistant", "content": "a different persisted text"},
             {"role": "user", "content": "next"}]
    assert chat_transcript.carried(32, other)[-2]["content"] == "Answer text."   # never swapped wholesale
