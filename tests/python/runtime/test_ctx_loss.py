# tests/python/runtime/test_ctx_loss.py
"""What a long chat must not lose: targets, the user's corrections, the
approaches that failed, the repositories it works in — through every condense,
restart, retry and message."""
import json
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_lessons as L
from aiforge_core.runtime import targets as T

JSON = '{"a": 1, "b": [1, 2, 3], "c": {"d": "e"}}' + " " * 200


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TARGETS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_LESSONS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(L, "_temporary", lambda sid: False)
    monkeypatch.setattr(L, "_repo_of", lambda sid: "")
    L._MEM.clear()


def _st(**kw):
    base = dict(goal="build the report", steers=[], file_hashes={}, cwd="/tmp",
                session_id=7, unlimited=False, readonly_mode=False, builder=None,
                item_resets=0, failed_approaches=[])
    base.update(kw)
    return SimpleNamespace(**base)


# ── targets ────────────────────────────────────────────────────────────────

def test_a_second_different_target_with_the_same_label_is_kept():
    T.capture(5, "expected output:\n```\n" + JSON + "\n```")
    T.capture(5, "expected output:\n```\nname,qty\nbolt,3\nnut,9\n```")
    rows = T.list(5)
    assert [r["id"] for r in rows] == ["target-1", "target-2"]


def test_two_payloads_in_one_message_are_two_targets():
    got = T.capture(5, "expected output:\n```\nA one\n```\nand also\n"
                       "expected output:\n```\nB two\n```")
    assert len(got) == 2 and len(T.list(5)) == 2


def test_a_restated_target_becomes_a_new_version():
    T.capture(5, "expected output:\n```\n" + JSON + "\n```")
    T.capture(5, "actually the expected output is:\n```\n" + JSON.replace('"e"', '"f"') + "\n```")
    rows = T.list(5)
    assert len(rows) == 1 and rows[0]["path"].endswith("target-1.v2.json")


def test_short_unfenced_payload_under_a_bare_cue_is_captured():
    got = T.capture(5, "expected output:\n42\n\nPlease fix calc.py.")
    assert got and open(got[0]["path"]).read().strip() == "42"


def test_an_instruction_under_a_bare_cue_is_not_captured():
    assert T.capture(5, "expected output:\nplease make it match the spec now.") == []


def test_a_bracketed_when_does_not_hedge_a_fenced_cue():
    got = T.capture(5, "expected output (after the fix):\n```\nok 3\n```")
    assert got


@pytest.mark.parametrize("msg", [
    "the function should return this output:\n```\n[1, 2]\n```",
    "here is the expected output\n```\n[1, 2]\n```",
])
def test_wider_cue_wordings(msg):
    assert T.capture(5, msg)


@pytest.mark.parametrize("msg", [
    "parse() should return None when the key is missing:\n```python\ndef parse(d):\n    return d['k']\n```",
    "the test fails:\nExpected: 3\nReceived: 4\n    at Object.<anonymous> (sum.test.js:3:20)\n" + "x" * 300,
    "expected output (roughly):\n```\nok 3\n```",
])
def test_code_test_logs_and_loose_matches_are_not_targets(msg):
    assert T.capture(5, msg) == []


def test_two_same_schema_targets_in_two_messages_stay_two():
    T.capture(5, "expected output:\n```\n" + JSON + "\n```")
    T.capture(5, "expected output:\n```\n" + JSON.replace('"e"', '"f"') + "\n```")
    assert len(T.list(5)) == 2


# ── lessons ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "that's the wrong repo, work in api/",
    "No, don't touch the tests",
    "you forgot to run the build again",
    "I told you to use pnpm",
    "never commit to main",
])
def test_corrections_are_recognised(msg):
    assert L.is_correction(msg)


@pytest.mark.parametrize("msg", [
    "add a login page", "what does this do?", "run the tests and show me",
    "what is wrong with the parser", "the build is broken", "reject incorrect passwords",
    "make sure you keep the old API", "if you can't find it, create it",
    "use pnpm instead of npm", "you already have access to the repo"])
def test_ordinary_requests_are_not_corrections(msg):
    assert not L.is_correction(msg)


def test_correction_is_kept_on_disk_and_rendered():
    L.note_correction(7, "that's the wrong repo, use api/", said="I edited web/x.ts")
    L.note_correction(7, "that's the wrong repo, use api/")      # repeat: one entry
    data = json.loads(open(L._path(7)).read())
    assert len(data["corrections"]) == 1
    b = L.block(7)
    assert b.startswith(L.MARK) and "use api/" in b and "I edited web/x.ts" in b


def test_failed_approaches_outlive_the_turn_and_live_ones_show():
    L.note_failed(7, ["pip install foo -> 404"])
    b = L.block(_st(failed_approaches=["npm ci -> EACCES"]))
    assert "pip install foo -> 404" in b and "npm ci -> EACCES" in b


def test_block_is_bounded():
    for i in range(30):
        L.note_correction(7, f"don't do thing {i} " + "x" * 450)
    assert len(L.block(7)) <= L._BLOCK_CHARS


def test_temporary_chat_keeps_lessons_in_memory_only(monkeypatch):
    monkeypatch.setattr(L, "_temporary", lambda sid: True)
    L.note_correction(9, "don't touch the db")
    assert "don't touch the db" in L.block(9)
    import os
    assert not os.path.exists(L._path(9))


def test_only_strong_corrections_go_to_the_project():
    assert L.is_strong_correction("that's the wrong repo")
    assert not L.is_strong_correction("don't commit yet")


def test_session_rules_are_kept_apart_and_undone():
    L.note_rule(7, "use tabs")
    for i in range(12):
        L.note_correction(7, f"don't touch file{i}.py")
    b = L.block(7)
    assert "rule the user set for this chat: use tabs" in b
    L.drop_rule(7, "use tabs")
    assert "use tabs" not in L.block(7)


def test_forget_takes_the_chat_out_of_the_project(monkeypatch):
    monkeypatch.setattr(L, "_repo_of", lambda sid: "shop")
    L._note_project("shop", {"text": "never edit migrations"}, 1)
    L.forget(1)
    assert L.project_lessons("shop") == []


def test_project_lessons_reach_a_new_chat(monkeypatch):
    monkeypatch.setattr(L, "_repo_of", lambda sid: "shop")
    L._note_project("shop", {"text": "never edit migrations"}, 1)
    assert "earlier chat" in L.block(2) and "never edit migrations" in L.block(2)
    assert "earlier chat" not in L.block(1)        # its own chat shows it as its own


def test_forget_drops_the_file():
    L.note_correction(7, "don't do that")
    L.forget(7)
    assert L.block(7) == ""


# ── the pin ────────────────────────────────────────────────────────────────

def test_turn_pin_carries_lessons_and_repositories(monkeypatch):
    from aiforge_core.runtime import repo_roles
    from aiforge_core.runtime.chat_agent._turn._tasks import turn_pin
    monkeypatch.setattr(repo_roles, "prompt_note", lambda sid, base=None, *a, **k:
                        "REPOSITORIES IN THIS CHAT (recorded):\n- WORK IN: /r/api")
    L.note_correction(7, "don't touch web/")
    pin = turn_pin(_st())
    assert pin.startswith("ORIGINAL TASK") and "WORK IN: /r/api" in pin
    assert "don't touch web/" in pin


def test_turn_pin_without_goal_still_keeps_targets_and_the_request():
    from aiforge_core.runtime.chat_agent._context._compaction import _pin_goal
    from aiforge_core.runtime.chat_agent._turn._tasks import turn_pin
    T.capture(7, "expected output:\n```\n" + JSON + "\n```")
    pin = turn_pin(_st(goal=""))
    assert pin and pin.startswith("KEEP IN VIEW") and "TARGETS" in pin
    convo = [{"role": "system", "content": "s"},
             {"role": "user", "content": "make the endpoint return this"}]
    out = _pin_goal("", convo, pin)
    assert "make the endpoint return this" in out and "TARGETS" in out


def test_turn_pin_keeps_more_steers():
    from aiforge_core.runtime.chat_agent._turn._tasks import turn_pin
    pin = turn_pin(_st(steers=[f"steer {i} " + "y" * 500 for i in range(10)]))
    assert "steer 2 " in pin and "steer 1 " not in pin
    assert "y" * 500 in pin


def test_repin_drops_the_stale_copies_of_pinned_blocks():
    from aiforge_core.runtime.chat_agent._turn._limits import _without_pinned_blocks
    text = ("<<AIFORGE_TURN_CONTEXT>>\nRULES: x\n\nTARGETS — data the user gave to "
            "match. ...\n- target-1 — verified: no\n\nLESSONS FROM THIS CHAT — old\n"
            "- the user said: \"a\"\n\nSKILL: y\n<<END_AIFORGE_TURN_CONTEXT>>")
    out = _without_pinned_blocks(text)
    assert "TARGETS" not in out and "LESSONS" not in out
    assert "RULES: x" in out and "SKILL: y" in out


def test_retry_shrink_pins_the_turn(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _completion as C
    from aiforge_core.runtime.chat_agent import _context
    seen = {}

    def fake(convo, **kw):
        seen.update(kw)
        return convo[:1] + [{"role": "user", "content": "short"}]
    monkeypatch.setattr(_context, "_compact_convo", fake)
    convo = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "x" * 500}] * 4
    st = _st(convo=convo, compact_key="k", batch_unread=False, batch_mark=0,
             board={}, turn_note_text="")
    assert C._shrink_for_retry(convo, "chat", None, 7, st)
    assert seen["pin"].startswith("ORIGINAL TASK") and seen["handoff_st"] is st


def test_escalation_condense_pins_the_turn(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _escalate as E
    from aiforge_core.runtime.chat_agent import _context
    seen = {}

    def fake(convo, **kw):
        seen.update(kw)
        return convo[:1]
    monkeypatch.setattr(_context, "_compact_convo", fake)
    st = _st(convo=[{"role": "system", "content": "s"}] * 5, role="chat",
             compact_key="k", batch_unread=False, batch_mark=0, board={},
             turn_note_text="")
    E._condense(st)
    assert seen["pin"].startswith("ORIGINAL TASK")


# ── summaries, handoff, learner, cues ──────────────────────────────────────

def test_a_cancelled_slice_is_folded_into_the_next_summary(monkeypatch):
    from aiforge_core.runtime.chat_agent._context import _compaction as C
    from aiforge_core.runtime.chat_agent._context import _summary_bg
    sent = []
    monkeypatch.setattr(C, "_compact_mode", lambda: "llm")
    monkeypatch.setattr(_summary_bg, "schedule", lambda k, g, call: True)
    monkeypatch.setattr(C, "_summary_messages",
                        lambda middle, prior="": sent.append(list(middle)) or [])
    first = [{"role": "user", "content": "slice one"}]
    second = [{"role": "user", "content": "slice two"}]
    monkeypatch.setattr(_summary_bg, "pending", lambda k: None)
    C._schedule_llm_summary(first, None, "run", 1)
    monkeypatch.setattr(_summary_bg, "pending", lambda k: 1)   # still writing #1
    C._schedule_llm_summary(second, None, "run", 2)
    assert sent[-1] == first + second
    C.release_run("run")
    assert "run" not in C._UNSUMMARISED


def test_handoff_keeps_steers_targets_and_repositories(monkeypatch):
    from aiforge_core.runtime import handoff, repo_roles
    monkeypatch.setattr(repo_roles, "prompt_note", lambda sid, base=None, *a, **k:
                        "REPOSITORIES IN THIS CHAT (recorded):\n- READ ONLY: /r/lib")
    T.capture(7, "expected output:\n```\n" + JSON + "\n```")
    st = _st(steers=["use the v2 endpoint"], board={}, convo=[])
    h = handoff.build_chat(st)
    text = handoff.render(h)
    assert "use the v2 endpoint" in text and "READ ONLY: /r/lib" in text
    assert "target-1" in text and "not yet compared" in text and ".json" in text


def test_learner_sees_the_error_of_a_failed_tool():
    from aiforge_core.runtime.chat_learner import _transcript
    steps = [{"type": "tool", "name": "run_command", "args": {"cmd": "pytest -q"},
              "result": {"ok": False, "stderr": "E   AssertionError: expected 3"}}]
    out = _transcript("that's wrong, fix the test", "done", steps)
    assert "pytest -q" in out and "AssertionError" in out
    assert "correcting an earlier answer" in out


def test_corrections_reach_the_rule_classifier():
    from aiforge_core.runtime.capture_cues import has_cue
    assert has_cue("that's the wrong repo")
    assert has_cue("I told you to use pnpm")
