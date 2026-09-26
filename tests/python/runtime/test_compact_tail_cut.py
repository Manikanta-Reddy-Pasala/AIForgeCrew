"""Where a condense cuts, and what it may put back."""
from aiforge_core.runtime import context_seen as seen
from aiforge_core.runtime.chat_agent._context import _compaction as comp
from aiforge_core.runtime.chat_agent._context import _tail_cut


def _roles(msgs):
    return [m.get("role") for m in msgs]


def _valid_after_system(msgs):
    """User first; every tool result follows its assistant tool_calls."""
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user", _roles(msgs)
    open_calls = set()
    for m in msgs[1:]:
        if m.get("role") == "assistant":
            open_calls = {c["id"] for c in m.get("tool_calls") or []}
        elif m.get("role") == "tool":
            assert m["tool_call_id"] in open_calls, _roles(msgs)


def test_a_text_mode_tail_never_opens_on_an_assistant_action(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "2000")
    monkeypatch.setenv("AIFORGE_COMPACT_MODE", "heuristic")
    convo = [{"role": "system", "content": "S" * 100}]
    for i in range(30):
        convo.append({"role": "assistant",
                      "content": f"THOUGHT: t\nACTION: file_read\nARGS_JSON: {i}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 200})
    # Whatever tail size the budget picks, the message after system is user.
    for keep in (4, 5, 8, 9):
        out = comp._compact_convo(list(convo), keep_recent=keep)
        assert "auto-condensed" in out[0]["content"]
        _valid_after_system(out)


def _native_convo(n=12, size=400):
    convo = [{"role": "system", "content": "S" * 100},
             {"role": "user", "content": "do the thing"}]
    for i in range(n):
        convo.append({"role": "assistant", "content": "",
                      "tool_calls": [{"id": f"c{i}a"}, {"id": f"c{i}b"}]})
        convo.append({"role": "tool", "tool_call_id": f"c{i}a",
                      "content": "r" * size})
        convo.append({"role": "tool", "tool_call_id": f"c{i}b",
                      "content": "r" * size})
    return convo


def test_a_native_tail_never_orphans_a_tool_result(monkeypatch):
    """Only one real user turn, far back: the cut cannot reach it, so it keeps
    the tool exchange whole and opens with a short harness note."""
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "3000")
    monkeypatch.setenv("AIFORGE_COMPACT_MODE", "heuristic")
    convo = _native_convo()
    for keep in (3, 4, 5, 6, 7):
        out = comp._compact_convo(list(convo), keep_recent=keep)
        assert "auto-condensed" in out[0]["content"]
        _valid_after_system(out)
        assert "not the user" in out[1]["content"]


def test_the_cut_reaches_back_to_a_nearby_user_turn():
    convo = [{"role": "system", "content": "s"},
             {"role": "user", "content": "a"},
             {"role": "assistant", "content": "b"},
             {"role": "user", "content": "c"},
             {"role": "assistant", "content": "d"},
             {"role": "user", "content": "e"}]
    start, opener = _tail_cut.tail_start(convo, 2)
    assert (start, opener) == (3, False)
    # No room for the extra message: keep the cut, add the note instead.
    convo[3]["content"] = "c" * 500
    start, opener = _tail_cut.tail_start(convo, 2, room=10)
    assert (start, opener) == (4, True)


def test_a_restored_body_that_does_not_fit_stays_a_pointer(monkeypatch, tmp_path):
    """Restoring after the budget check put the history back over budget,
    so the NEXT step condensed again, and again. Past the budget the pointer
    stays and the model can re-read."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "12000")
    monkeypatch.setenv("AIFORGE_COMPACT_MODE", "heuristic")
    brief = ("Follow these exact steps to deploy the hotfix, then run the "
             "smoke check before telling the user it is done.\n"
             + "\n".join(f"- step {i}: keep the page" for i in range(900)))
    sys = "CORE RULES\n" + seen.pointer("okf", "PROJECT MEMORY (repo)", brief)
    convo = [{"role": "system", "content": sys},
             {"role": "user", "content": "please add the export"},
             {"role": "assistant", "content":
              "--- MEMORY (prior facts / decisions / failures) ---\n" + brief}]
    for _ in range(12):
        convo.append({"role": "assistant", "content": "ACTION: file_read\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 900})
    assert len(brief) > 12000
    out = comp._compact_convo(convo, keep_recent=4)
    # Control: without the budget, the body would have come back.
    raw = seen.restore_dangling(out, convo[1:])
    assert brief in raw[0]["content"]
    assert "already in context:" in out[0]["content"]
    assert brief not in out[0]["content"]
    budget = 12000
    assert sum(len(m["content"]) for m in out[1:]) <= budget
    # And it is stable: the next step does not condense again.
    assert comp._compact_convo(out, keep_recent=4) is out


def test_within_budget_keeps_what_fits_per_message():
    before = [{"role": "system", "content": "p1"}, {"role": "user", "content": "p2"}]
    after = [{"role": "system", "content": "p1" + "A" * 50},
             {"role": "user", "content": "p2" + "B" * 10}]
    assert _tail_cut.within_budget(before, after, 100) is after
    out = _tail_cut.within_budget(before, after, 20)
    assert out[0] is before[0] and out[1] is after[1]
