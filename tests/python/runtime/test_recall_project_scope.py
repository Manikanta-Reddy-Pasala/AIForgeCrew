"""What a chat did in ONE repo must not be recalled into a chat in ANOTHER.

Live: a chat working in repo A was handed "Fixed, 1 passed" — the closing
message of a chat that had fixed something in repo B. It read that as its own
repo's state and went looking through other projects' folders.

The prior-chat source searched every session's messages and only REORDERED
them (own project first); with no chat of its own to show, repo B's message
ranked top. The scope is now decided from the session's stored folder, at the
search, and holds for every path that reads prior chats.
"""
from __future__ import annotations

import importlib

import pytest

OUTCOME_B = "Fixed, 1 passed. The bug was in billing/calc.py"


@pytest.fixture
def chats(monkeypatch, tmp_path):
    """A real chat store with one finished chat in repo B and an older one in
    repo A; the test's own chat is a NEW session in repo A."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.delenv("AIFORGE_AFM_REPO", raising=False)
    monkeypatch.delenv("AIFORGE_UMEM_CROSS_TASK", raising=False)
    import aiforge_core.runtime.chat_store as cs
    importlib.reload(cs)
    repo_a, repo_b = tmp_path / "shop", tmp_path / "billing"
    repo_a.mkdir()
    repo_b.mkdir()
    b = cs.create_session(title="fix billing test", cwd=str(repo_b))["id"]
    cs.add_message(b, "user", "fix the failing test in calc")
    cs.add_message(b, "assistant", OUTCOME_B)
    a_old = cs.create_session(title="shop tests", cwd=str(repo_a))["id"]
    cs.add_message(a_old, "assistant",
                   "the shop test suite is run with make test")
    a_new = cs.create_session(title="new", cwd=str(repo_a))["id"]
    cs.add_message(a_new, "user", "fix the failing test")
    plain = cs.create_session(title="no project")["id"]
    cs.add_message(plain, "assistant", "a failing test usually means a typo")
    return {"cs": cs, "a": str(repo_a), "b": str(repo_b),
            "a_new": a_new, "a_old": a_old, "b_sid": b, "plain": plain}


@pytest.fixture
def uq(monkeypatch):
    """unified_query with only the prior-chat source answering."""
    monkeypatch.setenv("AIFORGE_UMEM_CACHE_TTL", "0")
    monkeypatch.setenv("AIFORGE_UMEM_CHAT", "1")
    from aiforge_core.memory import backend_select
    from aiforge_core.memory import unified_query as _uq
    monkeypatch.setattr(backend_select, "embedded", lambda: False)
    monkeypatch.setattr(_uq, "_rerank_top", lambda hits, query: None)
    monkeypatch.setattr(_uq, "_guess_library", lambda _t: None)
    return _uq


def _texts(hits) -> str:
    return "\n".join(str(h.get("text") or h.get("content") or "") for h in hits)


# ── the store: a search scoped by the session's stored folder ─────────────

def test_unscoped_search_still_sees_every_session_and_says_whose(chats):
    hits = chats["cs"].search_messages("fix the failing test", limit=10)
    by = {h["session_id"]: h["project"] for h in hits}
    assert by[chats["b_sid"]] == "billing" and by[chats["a_old"]] == "shop"
    assert by[chats["plain"]] == "general"      # no folder = no project


def test_scoped_search_returns_only_that_projects_sessions(chats):
    hits = chats["cs"].search_messages("fix the failing test", limit=10,
                                       project="shop")
    assert hits and {h["session_id"] for h in hits} <= {chats["a_old"],
                                                        chats["a_new"]}
    assert "Fixed, 1 passed" not in _texts(hits)


def test_scoped_search_of_an_unknown_project_is_empty_not_everything(chats):
    assert chats["cs"].search_messages("fix the failing test",
                                       project="nowhere") == []


def test_scope_is_the_stored_folder_not_the_words(chats):
    """A repo-A chat that MENTIONS billing stays; the billing chat goes."""
    cs = chats["cs"]
    cs.add_message(chats["a_old"], "assistant",
                   "billing/calc.py is not part of this failing test run")
    hits = cs.search_messages("billing calc failing test", limit=10,
                              project="shop")
    assert any("not part of this" in h["content"] for h in hits)
    assert all(h["session_id"] != chats["b_sid"] for h in hits)


def test_old_project_chats_are_not_crowded_out_by_newer_ones_elsewhere(chats):
    """The candidate window is spent inside the project, not on the newest
    messages of every project."""
    cs = chats["cs"]
    for i in range(520):
        cs.add_message(chats["b_sid"], "assistant", f"failing test note {i}")
    hits = cs.search_messages("failing test", limit=5, project="shop")
    assert hits and all(h["project"] == "shop" for h in hits)


# ── recall into a chat (the bug) ──────────────────────────────────────────

def test_recall_in_repo_a_does_not_return_repo_b_chat_outcome(chats, uq):
    from aiforge_core.runtime.chat_agent._context import _recall
    args = _recall._recall_args(chats["a"], "fix the failing test", 6,
                                chats["a_new"])
    hits = _recall._run_recall(args)["hits"]
    assert "Fixed, 1 passed" not in _texts(hits)
    assert "billing" not in _texts(hits)
    assert "make test" in _texts(hits)           # its own project's chat stays


def test_recall_with_no_chat_of_its_own_returns_no_chat_at_all(chats, uq):
    """The case that leaked: nothing of its own, so the other project's
    message used to be the top (and only) prior-chat hit."""
    cs = chats["cs"]
    other = cs.create_session(title="x", cwd=chats["a"] + "-web")["id"]
    res = uq.query("fix the failing test", repo="shop-web",
                   exclude_session=other, cross_project=True)
    assert [h for h in res["hits"] if h.get("source") == "chat"] == []


def test_ticket_and_pipeline_recall_are_scoped_the_same_way(chats, uq):
    res = uq.query("fix the failing test", repo="shop")
    assert "Fixed, 1 passed" not in _texts(res["hits"])


def test_a_repo_less_search_still_reads_every_chat(chats, uq):
    res = uq.query("fix the failing test")
    assert "Fixed, 1 passed" in _texts(res["hits"])


def test_cross_task_opt_in_reads_every_chat(chats, uq, monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_CROSS_TASK", "1")
    res = uq.query("fix the failing test", repo="shop")
    assert "Fixed, 1 passed" in _texts(res["hits"])


def test_a_scoped_recall_never_retries_unscoped(monkeypatch):
    """A store that cannot scope gives nothing, not everybody's chats."""
    from aiforge_core.memory.unified_query import _sources
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "search_messages",
                        lambda text, limit=6: [{"session_id": 1, "role": "a",
                                                "content": OUTCOME_B}])
    with pytest.raises(TypeError):
        _sources._chat_sessions("fix", limit=4, project="shop")


def test_prior_chat_block_is_scoped_to_the_chats_project(chats):
    from aiforge_core.runtime.chat_agent._context import _recall
    block = _recall._chat_session_recall(
        "fix the failing test", chats["a_new"], cwd=chats["a"])
    assert "make test" in block and "Fixed, 1 passed" not in block
    assert _recall._chat_session_recall(
        "fix the failing test", chats["a_new"], cwd=chats["a"] + "-web") == ""


def test_a_subfolder_of_the_repo_is_the_same_project(chats, monkeypatch):
    from aiforge_core.runtime import repo_ident
    from aiforge_core.runtime.chat_agent._context import _recall
    sub = chats["a"] + "/src"
    monkeypatch.setattr(
        repo_ident, "git_toplevel",
        lambda cwd: chats["a"] if str(cwd).startswith(chats["a"]) else None)
    block = _recall._chat_session_recall(
        "fix the failing test", chats["a_new"], cwd=sub)
    assert "make test" in block and "Fixed, 1 passed" not in block


# ── the search tool ───────────────────────────────────────────────────────

def test_search_tool_reads_this_project_by_default(chats):
    from aiforge_core.runtime.chat_agent._tools import _memory as M
    out = M._t_search_chat_sessions({"query": "fix the failing test"},
                                    chats["a"])
    assert out["ok"] and out["hits"]
    assert "Fixed, 1 passed" not in _texts(out["hits"])


def test_search_tool_all_projects_labels_the_other_projects_hits(chats):
    from aiforge_core.runtime import chat_scope
    from aiforge_core.runtime.chat_agent._tools import _memory as M
    out = M._t_search_chat_sessions(
        {"query": "fix the failing test", "all_projects": True, "limit": 10},
        chats["a"])
    theirs = [h for h in out["hits"] if h["session_id"] == chats["b_sid"]]
    mine = [h for h in out["hits"] if h["session_id"] == chats["a_old"]]
    assert theirs and all(h["project"] == "billing"
                          and h["note"] == chat_scope.OTHER_PROJECT_NOTE
                          for h in theirs)
    assert mine and all("project" not in h and "note" not in h for h in mine)


# ── memory from another project: labelled, and kept out of the fold ───────

def test_other_project_memory_says_its_paths_do_not_apply_here():
    from aiforge_core.runtime.chat_agent._context._recall import _ranked_lines
    out = _ranked_lines([{"text": "billing retries five times",
                          "source": "doer", "project": "billing"}], 5)
    assert "project billing" in out and "do not apply to this one" in out


def test_memory_lookup_marks_a_hit_from_another_project(monkeypatch):
    from aiforge_core.memory import unified_query
    from aiforge_core.runtime import chat_scope
    from aiforge_core.runtime.chat_agent._tools import _memory as M
    monkeypatch.setattr(unified_query, "query", lambda q, **kw: {"hits": [
        {"text": "billing fact", "source": "cross", "project": "billing"},
        {"text": "shop fact", "source": "memory"}]})
    hits = M._t_memory_lookup({"query": "fact"}, "/r")["hits"]
    assert hits[0]["note"] == chat_scope.OTHER_PROJECT_NOTE
    assert "note" not in hits[1] and "project" not in hits[1]


def test_the_summary_never_absorbs_another_projects_memory(monkeypatch):
    """Folded into one briefing, the other project's fact would lose its label
    and read as this project's."""
    from aiforge_core.runtime.chat_agent._context import _recall
    hits = [{"text": f"shop fact {i}", "source": "memory"} for i in range(5)]
    hits.append({"text": "billing keeps calc in billing/calc.py",
                 "source": "cross", "project": "billing"})
    seen: dict = {}

    def _fold(q, rows):
        seen["rows"] = rows
        return "- shop briefing"

    monkeypatch.setattr(_recall, "_recall_hits", lambda *a: hits)
    monkeypatch.setattr(_recall, "_skip_recall_summary", lambda q: False)
    monkeypatch.setattr(_recall, "_summarised", _fold)
    monkeypatch.setattr("aiforge_core.runtime.chat_router.plain_chat",
                        lambda q: False)
    out = _recall._memory_recall("/r", "where is calc kept and how is it run")
    assert all(not h.get("project") for h in seen["rows"])
    assert "- shop briefing" in out
    assert "billing/calc.py" in out and "do not apply to this one" in out


# ── which project a session is in ─────────────────────────────────────────

def test_session_project_comes_from_the_stored_folder(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_scope
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scratch = tmp_path / "ws" / "session-7"
    scratch.mkdir(parents=True)
    repo = tmp_path / "shop"
    repo.mkdir()
    assert chat_scope.session_project(str(repo)) == "shop"
    assert chat_scope.session_project(str(scratch)) == "general"
    assert chat_scope.session_project(None) == "general"
    assert chat_scope.session_project("  ") == "general"
