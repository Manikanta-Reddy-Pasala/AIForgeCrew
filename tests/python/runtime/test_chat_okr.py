"""T4 — session-end OKR compaction.

At session end (idle / explicit) a session's transcript is distilled by the
learner LLM into atomic durable items (decisions, learnings, meaningful user
inputs — NOT chit-chat), each routed to its scope (global / project / topic) via
classify_scope and folded into the matching OKR brief through md_store.capture.
"""
from __future__ import annotations

import re
import types

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


_MSGS = [
    {"role": "user", "content": "thanks! also always run tests before commit"},
    {"role": "assistant", "content": "will do"},
    {"role": "user", "content": "OrderController maps /orders in svc"},
]


def test_compact_session_routes_items_to_scoped_briefs(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import chat_okr

    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: _MSGS)

    def _fake(role, messages, model, *a, **k):
        n = getattr(model, "__name__", "")
        if n == "ScopeDecisions":            # batched scope call: one per item
            return types.SimpleNamespace(items=[
                types.SimpleNamespace(
                    index=int(re.match(r"^\[(\d+)\] ", ln).group(1)),
                    scope="global" if "tests" in ln else "project",
                    repo="", topic="")
                for ln in messages[-1]["content"].splitlines()
                if re.match(r"^\[\d+\] ", ln)])
        # extraction
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(text="always run tests before commit",
                                  kind="learning"),
            types.SimpleNamespace(text="OrderController maps /orders",
                                  kind="project_learning")])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    res = chat_okr.compact_session("s1", repo="svc")
    assert res["ok"]
    assert res["captured"] == 2
    # global item promoted to shared, project item under its repo
    assert (md_store.brief_path("shared")).exists()
    assert (md_store.brief_path("svc")).exists()


def test_compact_session_skips_short(monkeypatch, mem):
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: [{"role": "user", "content": "hi"}])
    res = chat_okr.compact_session("s1", repo="svc", min_turns=4)
    assert res["ok"]
    assert res.get("skipped") == "too_short"


def test_compact_session_disabled(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT", "off")
    from aiforge_core.runtime import chat_okr
    res = chat_okr.compact_session("s1", repo="svc")
    assert res["skipped"] == "disabled"


def test_compact_session_soft_fails_on_llm_error(monkeypatch, mem):
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: _MSGS)

    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _boom)
    res = chat_okr.compact_session("s1", repo="svc")
    assert res["ok"]
    assert res["captured"] == 0


def test_previous_session_brief_returns_prior(monkeypatch, mem):
    from aiforge_core.runtime import chat_okr
    sessions = [{"id": 5, "cwd": None}, {"id": 4, "cwd": None}]  # newest first
    monkeypatch.setattr("aiforge_core.runtime.chat_store.list_sessions",
                        lambda: sessions)
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.get_messages",
        lambda sid: ([{"role": "user", "content": "we chose the SQLite backend"},
                      {"role": "assistant", "content": "done"}]
                     if sid == 4 else []))
    out = chat_okr.previous_session_brief(5)
    assert "SQLite" in out
    assert "4" in out


def test_previous_session_brief_empty_when_only_current(monkeypatch, mem):
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.list_sessions",
                        lambda: [{"id": 5}])
    assert chat_okr.previous_session_brief(5) == ""


def test_compact_session_skips_when_no_new_messages(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "0")
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: _MSGS)

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(text="a durable fact", kind="learning")])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    r1 = chat_okr.compact_session("s1", repo="svc")
    assert r1["captured"] == 1
    r2 = chat_okr.compact_session("s1", repo="svc")   # no new messages
    assert r2.get("skipped") == "no_new"
    assert r2["captured"] == 0


def test_compact_session_only_new_turns(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "0")
    from aiforge_core.runtime import chat_okr
    state = {"msgs": list(_MSGS)}
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: state["msgs"])
    seen = {}

    def _fake(role, messages, model, *a, **k):
        seen["last"] = messages[-1]["content"]
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(text="fact", kind="learning")])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    chat_okr.compact_session("s1", repo="svc")               # folds 3 msgs
    state["msgs"] = list(_MSGS) + [
        {"role": "user", "content": "BRAND NEW deploy uses systemd now"}]
    chat_okr.compact_session("s1", repo="svc")               # only the new one
    assert "BRAND NEW deploy" in seen["last"]
    assert "always run tests" not in seen["last"]            # old turns not re-sent


def test_clear_all_markers_wipes_offsets(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    import importlib
    import aiforge_core.runtime.chat_okr as okr
    importlib.reload(okr)
    okr._save_marker({"1": 40, "2": 12})
    assert okr._load_marker() == {"1": 40, "2": 12}
    okr.clear_all_markers()
    assert okr._load_marker() == {}      # a reset-reused id-1 won't skip folding


def test_transcript_takes_the_OLDEST_turns_that_fit(mem):
    """Head-first, and it reports how many turns went in.

    Tail-first lost the head for good: the offset then jumped past every turn,
    so a long day in one chat had ~7% of its transcript ever reach the model.
    """
    from aiforge_core.runtime import chat_okr
    turns = [{"role": "user", "content": "A" * 100},
             {"role": "assistant", "content": "B" * 100},
             {"role": "user", "content": "C" * 100}]
    text, taken, part = chat_okr._transcript(turns, 250)
    assert taken == 2
    assert part == 0
    assert "A" * 100 in text
    assert "C" * 100 not in text
    assert len(text) <= 250
    # a lone OVER-LIMIT turn is SLICED, not clipped-and-consumed: it stays at
    # the same offset with a part marker until every slice has been sent
    big = [{"role": "user", "content": "X" * 500}]
    text, taken, part = chat_okr._transcript(big, 100)
    assert taken == 0
    assert part == 94
    assert len(text) == 100
    text2, taken2, part2 = chat_okr._transcript(big, 100, start_char=part)
    assert "X" in text2
    assert taken2 == 0
    assert part2 == 188
    # the slices cover the turn — walk it to the end
    seen, guard = 0, 0
    while part and guard < 20:
        _t, taken_n, part = chat_okr._transcript(big, 100, start_char=part)
        seen += 1
        guard += 1
    assert taken_n == 1
    assert part == 0
    assert guard < 20


def test_compact_session_walks_a_long_session_window_by_window(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT_CHARS", "200")
    from aiforge_core.runtime import chat_okr
    msgs = [{"role": "user", "content": f"fact {i} " + "z" * 90} for i in range(6)]
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: msgs)
    seen: list = []

    def _fake(role, messages, model, *a, **k):
        n = getattr(model, "__name__", "")
        if n == "ScopeDecisions":          # batched scope call
            return types.SimpleNamespace(items=[
                types.SimpleNamespace(index=i, scope="global", repo="",
                                      topic="")
                for i in range(20)])
        seen.append(messages[-1]["content"])
        return types.SimpleNamespace(items=[])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)

    first = chat_okr.compact_session("walk", repo=None)
    assert first["remaining"] > 0                    # one window is not the day
    folds = 1
    while chat_okr.compact_session("walk", repo=None).get("remaining"):
        folds += 1
        assert folds < 10
    # every turn reached the model exactly once, oldest first
    assert "fact 0" in seen[0]
    assert "fact 5" in seen[-1]
    assert sum(t["content"] in "\n\n".join(seen) for t in msgs) == len(msgs)
    assert chat_okr.compact_session("walk", repo=None)["skipped"] == "no_new"


def test_llm_failure_does_not_advance_the_offset(monkeypatch, mem):
    """One provider hiccup must not mark a day's turns as folded."""
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: _MSGS)

    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _boom)
    res = chat_okr.compact_session("s9", repo="svc")
    assert res["skipped"] == "extract_failed"
    assert res["captured"] == 0

    captured: list = []

    def _fake(role, messages, model, *a, **k):
        n = getattr(model, "__name__", "")
        if n == "ScopeDecisions":          # batched scope call
            return types.SimpleNamespace(items=[
                types.SimpleNamespace(index=i, scope="global", repo="",
                                      topic="")
                for i in range(20)])
        captured.append(messages[-1]["content"])
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(text="always run tests before commit",
                                  kind="learning")])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    assert chat_okr.compact_session("s9", repo="svc")["captured"] == 1
    assert "always run tests before commit" in captured[0]   # same turns, retried


def test_empty_turns_are_consumed_not_re_walked(mem):
    """A blank turn produces no line; if it did not count toward `taken` the
    walk would re-read it forever."""
    from aiforge_core.runtime import chat_okr
    turns = [{"role": "user", "content": "   "},
             {"role": "user", "content": ""},
             {"role": "user", "content": "A" * 50}]
    text, taken, part = chat_okr._transcript(turns, 100)
    assert taken == 3
    assert part == 0
    assert "A" * 50 in text


def test_extract_failure_reports_the_whole_backlog_as_remaining(monkeypatch, mem):
    """The caller decides whether to keep walking from `remaining`; omitting it
    on failure reads as "nothing left" and hides the unfolded turns."""
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: _MSGS)

    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _boom)
    res = chat_okr.compact_session("s8", repo="svc")
    assert res["remaining"] == len(_MSGS)


def test_window_is_sized_from_the_role_window_not_a_flat_8000(monkeypatch, mem):
    from aiforge_core.runtime import chat_okr
    monkeypatch.delenv("AIFORGE_SESSION_COMPACT_CHARS", raising=False)
    auto = chat_okr._window_chars("learner")
    assert 8000 < auto <= chat_okr._WINDOW_CEILING      # bigger than the old flat cap
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT_CHARS", "3000")
    assert chat_okr._window_chars("learner") == 3000    # an explicit value wins


def test_scopes_are_classified_in_ONE_call_per_window(monkeypatch, mem):
    """Per-item scoping was ~90% of a fold's LLM traffic."""
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: _MSGS)
    seen: list = []

    def _fake(role, messages, model, *a, **k):
        n = getattr(model, "__name__", "")
        seen.append(n)
        if n == "ScopeDecisions":
            return types.SimpleNamespace(items=[
                types.SimpleNamespace(index=i, scope="project", repo="svc", topic="")
                for i in range(6)])
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(text=f"durable fact {i}", kind="learning")
            for i in range(6)])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    res = chat_okr.compact_session("batch", repo="svc")
    assert res["captured"] == 6
    assert seen.count("ScopeDecisions") == 1        # six items, ONE scope call


def test_batch_scope_falls_back_to_hints_when_the_model_says_nothing(monkeypatch,
                                                                     mem):
    from aiforge_core.memory import md_store
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")

    def _empty(role, messages, model, *a, **k):
        return types.SimpleNamespace(items=[])       # no verdicts at all

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _empty)
    out = md_store.classify_scopes(["a fact", "another"], hint_repo="svc")
    assert [o["scope"] for o in out] == ["project", "project"]
    assert [o["repo"] for o in out] == ["svc", "svc"]
    assert all(o["fallback"] for o in out)       # "the model never said" ≠ verdict


def test_batch_scope_honours_the_deterministic_switch(monkeypatch, mem):
    from aiforge_core.memory import md_store

    def _boom(*a, **k):
        raise AssertionError("no model call with AIFORGE_OKR_SCOPE_LLM=0")

    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "0")
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _boom)
    assert md_store.classify_scopes(["x"], hint_topic="sync")[0]["topic"] == "sync"


def test_batch_scope_is_robust_to_a_confused_index_list(monkeypatch, mem):
    """Out-of-range, duplicate and repeated indices must never hand one item
    another item's scope."""
    from aiforge_core.memory import md_store
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")

    def _messy(role, messages, model, *a, **k):
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(index=0, scope="global", repo="", topic=""),
            types.SimpleNamespace(index=0, scope="topic", repo="", topic="sync"),
            types.SimpleNamespace(index=99, scope="global", repo="", topic=""),
            types.SimpleNamespace(index=-2, scope="global", repo="", topic=""),
        ])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _messy)
    out = md_store.classify_scopes(["a universal lesson", "second", "third"],
                                   hint_repo="svc")
    assert out[0]["scope"] == "global"           # FIRST verdict for index 0 wins
    assert not out[0].get("fallback")
    for o in out[1:]:                            # no verdict → hints, MARKED so
        assert o["scope"] == "project"
        assert o["repo"] == "svc"
        assert o["fallback"] is True             # cleanup_reheal must not delete


def test_batch_scope_keeps_one_line_per_item(monkeypatch, mem):
    """A multi-line item would look like extra unnumbered items and slide every
    index after it."""
    from aiforge_core.memory import md_store
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    sent: list = []

    def _spy(role, messages, model, *a, **k):
        sent.append(messages[-1]["content"])
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(index=i, scope="project", repo="svc", topic="")
            for i in range(2)])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _spy)
    md_store.classify_scopes(["line one\nline two\nline three", "plain"],
                             hint_repo="svc")
    listing = [ln for ln in sent[0].splitlines() if ln.startswith("[")]
    assert len(listing) == 2                     # two items → two lines
    assert "line two" in listing[0]              # nothing dropped, just flattened


def test_the_whole_window_reaches_the_model(monkeypatch, mem):
    """There must be exactly ONE transcript cap. A second, smaller truncation
    inside _extract marks turns folded that the model never saw."""
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT_CHARS", "20000")
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "0")
    from aiforge_core.runtime import chat_okr
    turns = [{"role": "user", "content": f"turn {i} " + "q" * 900} for i in range(18)]
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: turns)
    sent: list = []

    def _fake(role, messages, model, *a, **k):
        sent.append(messages[-1]["content"])
        return types.SimpleNamespace(items=[])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    chat_okr.compact_session("wide", repo="svc")
    assert len(sent[0]) > 12000                  # no hidden 12k re-truncation
    marker = chat_okr._load_marker()
    folded = chat_okr._entry(marker, "wide")["offset"]
    assert f"turn {folded - 1} " in sent[0]      # every folded turn was shown
    assert f"turn {folded} " not in sent[0]      # and nothing beyond it


def test_an_oversized_turn_is_walked_in_slices_not_clipped(monkeypatch, mem):
    """A 100k tool dump used to be cut to one window and marked folded — 78% of
    it seen by nobody, with `remaining` reporting a healthy walk."""
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT_CHARS", "2000")
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "0")
    from aiforge_core.runtime import chat_okr
    big = "START " + "z" * 8000 + " ONE-999-END"
    turns = [{"role": "user", "content": "small first turn"},
             {"role": "assistant", "content": big}]
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: turns)
    sent: list = []

    def _fake(role, messages, model, *a, **k):
        sent.append(messages[-1]["content"])
        return types.SimpleNamespace(items=[])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    for _ in range(12):
        if not chat_okr.compact_session("big", repo="svc").get("remaining"):
            break
    blob = "".join(sent)
    assert "ONE-999-END" in blob                 # the TAIL of the big turn too
    assert chat_okr.compact_session("big", repo="svc")["skipped"] == "no_new"


def test_a_window_the_model_can_never_answer_is_skipped_eventually(monkeypatch,
                                                                   mem):
    """A deterministic extract failure (truncated output, filter, size limit)
    repeats forever at temperature 0 — the session must not wedge."""
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT_CHARS", "500")
    from aiforge_core.runtime import chat_okr
    turns = [{"role": "user", "content": f"turn {i} " + "y" * 100} for i in range(6)]
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: turns)

    def _boom(*a, **k):
        raise RuntimeError("always truncates")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _boom)
    monkeypatch.setattr(chat_okr, "_WINDOW_FAIL_SPACING_S", 0)   # failures a day apart
    assert chat_okr._MAX_WINDOW_FAILS <= 5                       # bounded, not "eventually"
    for n in range(chat_okr._MAX_WINDOW_FAILS):
        r = chat_okr.compact_session("stuck", repo="svc")
        assert r["skipped"] == "extract_failed"
        if n < chat_okr._MAX_WINDOW_FAILS - 1:       # still holding the turns
            assert chat_okr._entry(chat_okr._load_marker(), "stuck")["offset"] == 0
    # on the _MAX_WINDOW_FAILS-th failure the poison window is skipped
    assert chat_okr._entry(chat_okr._load_marker(), "stuck")["offset"] > 0


def test_a_burst_of_failures_counts_as_one(monkeypatch, mem):
    """Three chat switches inside one two-minute provider hiccup must not
    discard a window of turns that nothing is wrong with."""
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT_CHARS", "500")
    from aiforge_core.runtime import chat_okr
    turns = [{"role": "user", "content": f"turn {i} " + "y" * 100} for i in range(6)]
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: turns)

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _boom)
    for _ in range(chat_okr._MAX_WINDOW_FAILS + 3):
        chat_okr.compact_session("hiccup", repo="svc")
    e = chat_okr._entry(chat_okr._load_marker(), "hiccup")
    assert e["fails"] == 1
    assert e["offset"] == 0


def test_a_window_whose_captures_all_fail_does_not_advance(monkeypatch, mem):
    """The store being down must not mark a window folded with zero captures —
    the write-side twin of the extract-failure guard."""
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: _MSGS)

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(text="a durable fact", kind="learning")])

    def _down(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    monkeypatch.setattr(md_store, "capture", _down)
    r = chat_okr.compact_session("wfail", repo="svc")
    assert r["skipped"] == "capture_failed"
    assert r["captured"] == 0
    assert chat_okr._entry(chat_okr._load_marker(), "wfail")["offset"] == 0

    captured: list = []
    monkeypatch.setattr(md_store, "capture",
                        lambda *a, **k: captured.append(a) or None)
    assert chat_okr.compact_session("wfail", repo="svc")["captured"] == 1


def test_scope_failure_does_not_abort_the_fold(monkeypatch, mem):
    """compact_session must never raise, and one scope failure must not discard
    a whole window's extracted items."""
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: _MSGS)

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(text="a durable fact", kind="learning")])

    def _boom(*a, **k):
        raise RuntimeError("scope guard blew up")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    monkeypatch.setattr(md_store, "classify_scopes", _boom)
    res = chat_okr.compact_session("scopefail", repo="svc")
    assert res["ok"]
    assert res["captured"] == 1


def test_batch_scope_chunks_beyond_the_batch_size(monkeypatch, mem):
    """>_BATCH_MAX items must chunk without dropping or duplicating any."""
    from aiforge_core.memory import md_store
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    n = md_store._scope._BATCH_MAX * 2 + 7
    calls: list = []

    def _fake(role, messages, model, *a, **k):
        lines = [ln for ln in messages[-1]["content"].splitlines()
                 if re.match(r"^\[\d+\] ", ln)]
        calls.append(len(lines))
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(index=int(re.match(r"^\[(\d+)\] ", ln).group(1)),
                                  scope="topic", repo="",
                                  topic=ln.split()[1])      # echo the item's id
            for ln in lines])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    out = md_store.classify_scopes([f"item-{i} body" for i in range(n)])
    assert len(out) == n
    assert sum(calls) == n
    assert [o["topic"] for o in out] == [f"item-{i}" for i in range(n)]


def test_previous_session_brief_skips_other_project(monkeypatch, mem, tmp_path):
    """A prior session in a DIFFERENT working tree is not "the previous
    session" — that carry-forward is how one chat's task became the next
    chat's work (two unpinned chats each own a chat-workspaces/session-N dir).
    """
    from aiforge_core.runtime import chat_okr
    other = tmp_path / "session-72"
    mine = tmp_path / "session-73"
    other.mkdir()
    mine.mkdir()
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.list_sessions",
        lambda: [{"id": 72, "cwd": str(other)}])
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.get_messages",
        lambda sid: [{"role": "user", "content": "fix the gpsd ublox config"}])
    assert chat_okr.previous_session_brief(73, cwd=str(mine)) == ""


def test_previous_session_brief_carries_same_project(monkeypatch, mem, tmp_path):
    from aiforge_core.runtime import chat_okr
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.list_sessions",
        lambda: [{"id": 9, "cwd": str(tmp_path / "elsewhere")},
                 {"id": 8, "cwd": str(repo)}])
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.get_messages",
        lambda sid: ([{"role": "user", "content": "we chose the SQLite backend"}]
                     if sid == 8 else
                     [{"role": "user", "content": "unrelated gpsd work"}]))
    out = chat_okr.previous_session_brief(10, cwd=str(repo))
    assert "SQLite" in out
    assert "gpsd" not in out                      # the other project's session
    assert "REFERENCE ONLY" in out
    assert "do NOT resume" in out.replace("Do NOT resume", "do NOT resume")


def test_previous_session_id_applies_same_cwd_filter(monkeypatch, mem, tmp_path):
    """previous_session_id picks what the brief picked — otherwise recall drops
    a session that was never injected."""
    from aiforge_core.runtime import chat_okr
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.list_sessions",
        lambda: [{"id": 9, "cwd": str(tmp_path / "elsewhere")},
                 {"id": 8, "cwd": str(repo)}])
    assert chat_okr.previous_session_id(10, cwd=str(repo)) == 8
    assert chat_okr.previous_session_id(10) == 9          # unfiltered, as before
