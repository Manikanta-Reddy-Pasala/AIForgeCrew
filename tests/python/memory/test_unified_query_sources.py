"""Unified recall: eleven sources, one ranked answer.

Every source is independently soft-failing — a broken one records its error
and the rest still answer — because recall runs on the hot path of every turn
and must degrade rather than raise.

Two rules carry most of the value. Scores are min-max normalised PER SOURCE
before their weight applies, or a fixed-score source (a ticket brief always
scores 1.0) buries a genuinely relevant cosine hit. And a repo-SCOPED call
keeps its vector recall scoped: an unscoped one let a game project's memory
leak into an unrelated converter, which is what AIFORGE_UMEM_CROSS_TASK now
gates.

The pre-dedup ranked list is kept alongside the final hits so the UI can show
each channel's own results — the flat list collapses a brief that matched both
the vector KNN and the keyword index into a single copy.
"""
from __future__ import annotations

import pytest

from aiforge_core.memory.unified_query import _query as Q


def _ctx(monkeypatch, **over):
    """A recall context whose package helpers are all stubbed."""
    import aiforge_core.memory.unified_query as pkg
    monkeypatch.setattr(pkg, "_tag",
                        lambda rows, source=None, weight=1.0:
                        [{**r, "source": source, "_weight": weight} for r in rows],
                        raising=False)
    args = {"text": "how does sync work", "ticket": None, "role": "chat",
            "limit": 5, "repo": "AIForgeCrew", "exclude_session": None,
            "boost_tags": None,
            "weights": {k: 1.0 for k in ("memory", "keyword", "recent",
                                         "ticket", "related", "symbol",
                                         "graphify", "doc", "external",
                                         "vector", "chat")},
            "pkg": pkg}
    args.update(over)
    return Q._RecallCtx(**args)


@pytest.fixture
def embedded(monkeypatch):
    from aiforge_core.memory import backend_select
    state = {"embedded": True}
    monkeypatch.setattr(backend_select, "embedded", lambda: state["embedded"])
    return state


# ─── the context ───────────────────────────────────────────────────────


def test_a_ticket_key_in_the_text_is_picked_up(monkeypatch):
    ctx = _ctx(monkeypatch, text="what broke in ONE-42?")
    assert ctx.auto_ticket == "ONE-42"


def test_an_explicit_ticket_wins(monkeypatch):
    ctx = _ctx(monkeypatch, text="ONE-42", ticket="ONE-9")
    assert ctx.auto_ticket == "ONE-9"


def test_the_repo_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("AIFORGE_AFM_REPO", "PosFrontend")
    assert _ctx(monkeypatch, repo=None)._repo_or_env() == "PosFrontend"
    assert _ctx(monkeypatch, repo="Given")._repo_or_env() == "Given"


def test_no_repo_anywhere_is_a_global_recall(monkeypatch):
    monkeypatch.delenv("AIFORGE_AFM_REPO", raising=False)
    assert _ctx(monkeypatch, repo=None)._repo_or_env() is None


# ─── the embedded sources ──────────────────────────────────────────────


def test_the_agents_own_observations_are_recalled(monkeypatch, embedded):
    from aiforge_core.memory import sqlite_memory
    seen: dict = {}
    monkeypatch.setattr(sqlite_memory, "recall",
                        lambda text, limit=None, repo=None, boost_tags=None:
                        seen.update(text=text, repo=repo)
                        or [{"text": "sync retries three times"}])
    ctx = _ctx(monkeypatch)
    Q._src_sqlite_recall(ctx)
    assert ctx.used == ["memory"]
    assert ctx.raw_hits[0]["source"] == "memory"
    assert seen["repo"] == "AIForgeCrew"


def test_keyword_search_catches_what_embeddings_blur(monkeypatch, embedded):
    """Exact ids, service names and hashes."""
    from aiforge_core.memory import sqlite_memory
    monkeypatch.setattr(sqlite_memory, "keyword_search",
                        lambda text, repo=None, limit=None:
                        [{"text": "ONE-42 fixed in a1b2c3d"}])
    ctx = _ctx(monkeypatch)
    Q._src_keyword(ctx)
    assert ctx.used == ["keyword"]


def test_a_just_written_fact_surfaces_before_it_is_embedded(monkeypatch,
                                                            embedded):
    from aiforge_core.memory import sqlite_memory
    seen: dict = {}
    monkeypatch.setattr(sqlite_memory, "recent",
                        lambda limit=None, repo=None: seen.update(limit=limit)
                        or [{"text": "just captured"}])
    monkeypatch.setenv("AIFORGE_UMEM_RECENT_N", "3")
    ctx = _ctx(monkeypatch)
    Q._src_recent(ctx)
    assert ctx.used == ["recent"]
    assert seen["limit"] == 3


def test_the_hot_cache_can_be_switched_off(monkeypatch, embedded):
    from aiforge_core.memory import sqlite_memory
    monkeypatch.setenv("AIFORGE_UMEM_RECENT", "0")
    monkeypatch.setattr(sqlite_memory, "recent",
                        lambda **kw: pytest.fail("read the hot cache"))
    ctx = _ctx(monkeypatch)
    Q._src_recent(ctx)
    assert ctx.used == []


def test_a_junk_hot_cache_size_falls_back(monkeypatch, embedded):
    from aiforge_core.memory import sqlite_memory
    monkeypatch.setenv("AIFORGE_UMEM_RECENT_N", "many")
    seen: dict = {}
    monkeypatch.setattr(sqlite_memory, "recent",
                        lambda limit=None, repo=None: seen.update(limit=limit)
                        or [])
    Q._src_recent(_ctx(monkeypatch))
    assert seen["limit"] == 5


def test_without_the_embedded_backend_those_sources_stay_quiet(monkeypatch,
                                                               embedded):
    embedded["embedded"] = False
    ctx = _ctx(monkeypatch)
    Q._src_sqlite_recall(ctx)
    Q._src_keyword(ctx)
    Q._src_recent(ctx)
    assert ctx.used == []
    assert ctx.errors == []


def test_a_broken_source_records_its_error_and_answers_nothing(monkeypatch,
                                                               embedded):
    from aiforge_core.memory import sqlite_memory
    monkeypatch.setattr(sqlite_memory, "recall",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("db")))
    ctx = _ctx(monkeypatch)
    Q._src_sqlite_recall(ctx)
    assert ctx.used == []
    assert ctx.errors == ["memory: db"]


# ─── the ticket, symbol and doc sources ────────────────────────────────


def test_a_ticket_brief_is_recalled_at_full_score(monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    monkeypatch.setattr(pkg, "_ticket_brief",
                        lambda key: {"text": f"brief for {key}"},
                        raising=False)
    ctx = _ctx(monkeypatch, ticket="ONE-42")
    Q._src_ticket(ctx)
    assert ctx.used == ["ticket"]
    assert ctx.raw_hits[0]["_raw_score"] == 1.0


def test_no_ticket_means_no_brief(monkeypatch):
    ctx = _ctx(monkeypatch, text="how does sync work")
    Q._src_ticket(ctx)
    assert ctx.used == []


def test_a_symbol_query_looks_the_symbol_up(monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    seen: dict = {}
    monkeypatch.setattr(pkg, "_looks_like_symbol", lambda t: True,
                        raising=False)
    monkeypatch.setattr(pkg, "_extract_symbol", lambda t: "publishToRemote",
                        raising=False)
    monkeypatch.setattr(pkg, "_mcp_call",
                        lambda name, args: seen.update(name=name, args=args)
                        or [{"text": "found it"}], raising=False)
    monkeypatch.setattr(pkg, "_unpack_mcp_rows", lambda rows: rows,
                        raising=False)
    ctx = _ctx(monkeypatch)
    Q._src_symbol(ctx)
    assert ctx.used == ["symbol"]
    assert seen["args"]["query"] == "publishToRemote"


def test_a_prose_query_is_not_a_symbol_lookup(monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    monkeypatch.setattr(pkg, "_looks_like_symbol", lambda t: False,
                        raising=False)
    monkeypatch.setattr(pkg, "_mcp_call",
                        lambda *a: pytest.fail("looked up a symbol"),
                        raising=False)
    ctx = _ctx(monkeypatch)
    Q._src_symbol(ctx)
    assert ctx.used == []


def test_related_memories_are_keyed_by_ticket_symbol_or_text(monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    seen: dict = {}
    monkeypatch.setattr(pkg, "_looks_like_symbol", lambda t: False,
                        raising=False)
    monkeypatch.setattr(pkg, "_mcp_call",
                        lambda name, args: seen.update(key=args["key"])
                        or [{"text": "related"}], raising=False)
    monkeypatch.setattr(pkg, "_unpack_mcp_rows", lambda rows: rows,
                        raising=False)
    Q._src_related(_ctx(monkeypatch, ticket="ONE-42"))
    assert seen["key"] == "ONE-42"
    Q._src_related(_ctx(monkeypatch, text="plain question"))
    assert seen["key"] == "plain question"


def test_a_document_lookup_asks_for_three(monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    seen: dict = {}
    monkeypatch.setattr(pkg, "_mcp_call",
                        lambda name, args: seen.update(name=name, args=args)
                        or [{"text": "the doc"}], raising=False)
    monkeypatch.setattr(pkg, "_unpack_mcp_rows", lambda rows: rows,
                        raising=False)
    ctx = _ctx(monkeypatch)
    Q._src_doc(ctx)
    assert seen["args"] == {"query": ctx.text, "k": 3}


def test_external_docs_are_fetched_for_a_guessed_library(monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    monkeypatch.setattr(pkg, "_guess_library", lambda t: "fastapi",
                        raising=False)
    monkeypatch.setattr(pkg, "_docs_lookup",
                        lambda lib, text, top_k=None: [{"text": "docs"}],
                        raising=False)
    ctx = _ctx(monkeypatch)
    Q._src_external(ctx)
    assert ctx.used == ["external:fastapi"]


def test_no_library_means_no_external_lookup(monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    monkeypatch.setattr(pkg, "_guess_library", lambda t: None, raising=False)
    monkeypatch.setattr(pkg, "_docs_lookup",
                        lambda *a, **k: pytest.fail("fetched docs"),
                        raising=False)
    ctx = _ctx(monkeypatch)
    Q._src_external(ctx)
    assert ctx.used == []


# ─── the concept graph ─────────────────────────────────────────────────


def test_graph_matches_and_neighbours_become_rows():
    rows = Q._graphify_rows({
        "matches": [{"label": "SyncService", "source_file": "sync.py",
                     "id": "n1"}],
        "neighbors": [{"node": {"label": "Publisher"}, "relation": "calls",
                       "weight": 0.7},
                      {"node": None, "relation": "x"}]})
    assert rows[0]["text"] == "SyncService — sync.py"
    assert rows[0]["id"] == "n1"
    assert rows[1] == {"text": "Publisher (calls)", "score": 0.7}
    assert rows[2]["text"] == " (x)", "a label-less neighbour keeps its relation"


def test_a_wholly_empty_row_is_dropped():
    assert Q._graphify_rows({"matches": [{"label": "", "source_file": ""}],
                             "neighbors": []}) == []


@pytest.mark.parametrize("payload", [{}, {"matches": None, "neighbors": None},
                                     {"matches": {"a": 1}, "neighbors": "xs"}])
def test_a_payload_that_is_not_a_sequence_yields_no_rows(payload):
    """`gr` is a tool result, so neither key is guaranteed to be a list — a
    dict or a string used to be sliced as if it were one."""
    assert Q._graphify_rows(payload) == []


def test_the_graph_rows_are_bounded():
    rows = Q._graphify_rows({
        "matches": [{"label": f"m{i}"} for i in range(10)],
        "neighbors": [{"node": {"label": f"n{i}"}} for i in range(20)]})
    assert len(rows) == 6 + 12


def test_the_graph_is_read_without_the_agent_asking(monkeypatch):
    from aiforge_core.runtime import graphify_lookup_tool
    monkeypatch.setattr(graphify_lookup_tool, "graphify_lookup",
                        lambda text, hops=1, max_neighbors=12:
                        {"ok": True, "matches": [{"label": "SyncService"}]})
    ctx = _ctx(monkeypatch)
    Q._src_graphify(ctx)
    assert ctx.used == ["graphify"]


def test_no_graph_means_no_rows(monkeypatch):
    from aiforge_core.runtime import graphify_lookup_tool
    monkeypatch.setattr(graphify_lookup_tool, "graphify_lookup",
                        lambda *a, **k: {"ok": False})
    ctx = _ctx(monkeypatch)
    Q._src_graphify(ctx)
    assert ctx.used == []


def test_a_broken_graph_lookup_soft_fails(monkeypatch):
    from aiforge_core.runtime import graphify_lookup_tool
    monkeypatch.setattr(graphify_lookup_tool, "graphify_lookup",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    ctx = _ctx(monkeypatch)
    Q._src_graphify(ctx)
    assert ctx.errors
    assert ctx.used == []


# ─── the contamination guard ───────────────────────────────────────────


@pytest.fixture
def vector(monkeypatch, embedded):
    import aiforge_core.memory.unified_query as pkg
    embedded["embedded"] = False          # the global vector path
    seen: dict = {}
    monkeypatch.setattr(pkg, "_global_vector_recall",
                        lambda text, limit=None, repo=None:
                        seen.update(repo=repo) or [{"text": "a hit"}],
                        raising=False)
    return seen


def test_a_scoped_task_keeps_its_vector_recall_scoped(vector, monkeypatch):
    """Unscoped, a game project's memory leaked into an unrelated converter."""
    ctx = _ctx(monkeypatch, repo="tempconv")
    Q._src_global_vector(ctx)
    assert vector["repo"] == "tempconv"
    assert ctx.used == ["vector"]


def test_a_repo_less_call_searches_globally(vector, monkeypatch):
    Q._src_global_vector(_ctx(monkeypatch, repo=None))
    assert vector["repo"] is None


def test_cross_repo_bleed_is_opt_in(vector, monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_CROSS_TASK", "1")
    Q._src_global_vector(_ctx(monkeypatch, repo="tempconv"))
    assert vector["repo"] is None


def test_the_global_vector_source_can_be_switched_off(vector, monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_GLOBAL_VECTOR", "0")
    ctx = _ctx(monkeypatch)
    Q._src_global_vector(ctx)
    assert ctx.used == []
    assert "repo" not in vector


# ─── prior conversations ───────────────────────────────────────────────


@pytest.fixture
def chat(monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    seen: dict = {}
    monkeypatch.setattr(pkg, "_chat_sessions",
                        lambda text, limit=None, exclude_session=None:
                        seen.update(exclude=exclude_session)
                        or [{"text": "we chose postgres"}], raising=False)
    return seen


def test_prior_chats_inform_a_scoped_recall_too(chat, monkeypatch):
    ctx = _ctx(monkeypatch, exclude_session=9)
    Q._src_chat(ctx)
    assert ctx.used == ["chat"]
    assert chat["exclude"] == 9, \
        "and the live turn is not recalled as prior chat"


def test_chat_recall_can_be_switched_off_entirely(chat, monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_CHAT", "0")
    ctx = _ctx(monkeypatch)
    Q._src_chat(ctx)
    assert ctx.used == []


def test_chat_recall_can_be_switched_off_for_scoped_calls(chat, monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_CHAT_SCOPED", "0")
    ctx = _ctx(monkeypatch, repo="AIForgeCrew")
    Q._src_chat(ctx)
    assert ctx.used == []
    ctx = _ctx(monkeypatch, repo=None)
    Q._src_chat(ctx)
    assert ctx.used == ["chat"], "a global call still reads them"


# ─── fusing the sources ────────────────────────────────────────────────


@pytest.fixture
def fuse(monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    state: dict = {"reranked": None}
    monkeypatch.setattr(pkg, "_normalize_scores", lambda hits: hits,
                        raising=False)
    monkeypatch.setattr(pkg, "_dedup",
                        lambda hits: [h for i, h in enumerate(hits)
                                      if h.get("text") not in
                                      {x.get("text") for x in hits[:i]}],
                        raising=False)
    monkeypatch.setattr(pkg, "_diversify", lambda hits: hits, raising=False)
    monkeypatch.setattr(pkg, "_rerank_top",
                        lambda hits, query=None: state["reranked"],
                        raising=False)
    return state


def test_the_ranked_view_keeps_what_dedup_collapses(fuse, monkeypatch):
    """A brief that matched BOTH the vector KNN and the keyword index shows as
    one hit but two channel results."""
    ctx = _ctx(monkeypatch)
    ctx.raw_hits = [{"text": "same", "score": 0.9, "source": "memory"},
                    {"text": "same", "score": 0.5, "source": "keyword"}]
    top, ranked = Q._fuse_and_rank(ctx)
    assert len(top) == 1
    assert len(ranked) == 2


def test_the_hits_come_back_in_score_order(fuse, monkeypatch):
    ctx = _ctx(monkeypatch)
    ctx.raw_hits = [{"text": "low", "score": 0.1}, {"text": "high", "score": 0.9}]
    top, _ = Q._fuse_and_rank(ctx)
    assert [h["text"] for h in top] == ["high", "low"]


def test_the_answer_is_limited(fuse, monkeypatch):
    ctx = _ctx(monkeypatch, limit=2)
    ctx.raw_hits = [{"text": f"h{i}", "score": i / 10} for i in range(6)]
    top, ranked = Q._fuse_and_rank(ctx)
    assert len(top) == 2
    assert len(ranked) == 6


def test_a_reranker_that_answers_is_recorded(fuse, monkeypatch):
    fuse["reranked"] = [{"text": "best", "score": 1.0}]
    ctx = _ctx(monkeypatch)
    ctx.raw_hits = [{"text": "a", "score": 0.2}]
    top, _ = Q._fuse_and_rank(ctx)
    assert top[0]["text"] == "best"
    assert "reranker" in ctx.used


def test_every_ranking_stage_soft_fails(fuse, monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    for name in ("_normalize_scores", "_dedup", "_rerank_top"):
        monkeypatch.setattr(pkg, name,
                            lambda *a, **k: (_ for _ in ()).throw(OSError("x")),
                            raising=False)
    ctx = _ctx(monkeypatch)
    ctx.raw_hits = [{"text": "a", "score": 0.5}]
    top, _ = Q._fuse_and_rank(ctx)
    assert [h["text"] for h in top] == ["a"]
    assert len(ctx.errors) == 3


# ─── following the links ───────────────────────────────────────────────


@pytest.fixture
def links(monkeypatch):
    from aiforge_core.memory import md_store
    state: dict = {"linked": [{"text": "the neighbour brief", "source": "b.md",
                               "kind": "knowledge", "title": "B",
                               "file": "b.md"}]}
    monkeypatch.setattr(md_store, "expand_links",
                        lambda srcs, max_links=None: state["linked"])
    return state


def test_a_hits_neighbours_come_with_it(links, monkeypatch):
    add = Q._linked_additions(_ctx(monkeypatch), [{"source": "a.md",
                                                   "text": "the hit"}])
    assert add[0]["channel"] == "linked"
    assert add[0]["linked"] is True
    assert add[0]["source_uri"] == "linked://b.md"


def test_a_neighbour_already_in_the_answer_is_not_repeated(links, monkeypatch):
    add = Q._linked_additions(_ctx(monkeypatch),
                              [{"source": "a.md", "text": "the neighbour brief"}])
    assert add == []


def test_hits_with_no_source_have_no_links_to_follow(links, monkeypatch):
    assert Q._linked_additions(_ctx(monkeypatch), [{"text": "x"}]) == []


def test_link_expansion_can_be_switched_off(links, monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_LINK_EXPAND", "0")
    assert Q._linked_additions(_ctx(monkeypatch), [{"source": "a.md"}]) == []


def test_a_broken_expansion_never_breaks_recall(links, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "expand_links",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    ctx = _ctx(monkeypatch)
    assert Q._linked_additions(ctx, [{"source": "a.md"}]) == []
    assert ctx.errors
    assert ctx.errors[0].startswith("linked:")


# ─── rendering for a prompt ────────────────────────────────────────────


def test_the_block_names_its_sources_and_scores():
    out = Q.render({"used_sources": ["memory", "chat"],
                    "hits": [{"source": "memory", "text": "a fact",
                              "score": 0.87}]})
    assert "sources used: memory, chat" in out
    assert "1. [memory|0.87] a fact" in out


def test_a_recall_with_nothing_says_so():
    assert Q.render({"hits": []}) == "[unified_memory] no hits"


def test_errors_are_shown_under_the_hits():
    out = Q.render({"hits": [{"text": "x"}], "errors": ["memory: db"]})
    assert "[errors] memory: db" in out


def test_an_unscorable_hit_still_renders():
    out = Q.render({"hits": [{"text": "x", "score": "not a number"}]})
    assert "[?|0.00] x" in out


def test_a_long_hit_is_trimmed_onto_one_line():
    out = Q.render({"hits": [{"text": "a\nb" + "z" * 400}]})
    assert "\n" not in out.split("1. ")[1]
    assert len(out.split("] ")[1]) <= 300


# ─── the whole query ───────────────────────────────────────────────────


@pytest.fixture
def whole(monkeypatch, fuse):
    import aiforge_core.memory.unified_query as pkg
    monkeypatch.setattr(pkg, "_resolve_weights",
                        lambda: {k: 1.0 for k in
                                 ("memory", "keyword", "recent", "ticket",
                                  "related", "symbol", "graphify", "doc",
                                  "external", "vector", "chat")},
                        raising=False)
    monkeypatch.setattr(pkg, "_qcache_ttl", lambda: 0, raising=False)
    monkeypatch.setattr(Q, "_RECALL_SOURCES", ())
    Q._QCACHE.clear()
    yield
    Q._QCACHE.clear()


def test_an_empty_question_asks_nothing(whole):
    assert Q.query("   ") == {"hits": [], "used_sources": [], "errors": []}


def test_a_query_returns_hits_sources_and_the_ranked_view(whole, monkeypatch):
    monkeypatch.setattr(Q, "_RECALL_SOURCES",
                        (lambda ctx: ctx.raw_hits.append(
                            {"text": "a fact", "score": 0.9,
                             "source": "memory"}) or ctx.used.append("memory"),))
    out = Q.query("how does sync work", repo="AIForgeCrew")
    assert out["hits"][0]["text"] == "a fact"
    assert out["used_sources"] == ["memory"]
    assert out["ranked"]
    assert out["query"] == "how does sync work"


def test_the_current_session_is_excluded_by_either_name(whole, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(Q, "_RECALL_SOURCES",
                        (lambda ctx: seen.update(exclude=ctx.exclude_session),))
    Q.query("x", session_id=4)
    assert seen["exclude"] == 4
    Q.query("y", exclude_session=9, session_id=4)
    assert seen["exclude"] == 9


def test_an_identical_question_is_answered_from_cache(whole, monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    calls = {"n": 0}
    monkeypatch.setattr(pkg, "_qcache_ttl", lambda: 60, raising=False)
    monkeypatch.setattr(Q, "_RECALL_SOURCES",
                        (lambda ctx: calls.update(n=calls["n"] + 1),))
    Q.query("how does sync work", repo="r")
    Q.query("How does sync work", repo="r")
    assert calls["n"] == 1, "case-insensitive, same scope"
    Q.query("how does sync work", repo="other")
    assert calls["n"] == 2, "a different scope is a different question"


def test_the_cache_cannot_grow_without_bound(whole, monkeypatch):
    import aiforge_core.memory.unified_query as pkg
    monkeypatch.setattr(pkg, "_qcache_ttl", lambda: 60, raising=False)
    monkeypatch.setattr(Q, "_QCACHE_MAX", 3)
    for i in range(4):
        Q.query(f"question {i}")
    assert len(Q._QCACHE) <= 3


# ─── the sources this build no longer has ──────────────────────────────


def test_the_ticket_sources_answer_nothing_on_this_build():
    """Both ticket backends went with the SQLite-only build. The functions
    stay so recall's call site is untouched, but they say so plainly instead
    of routing through shims that could never return a row."""
    import aiforge_core.memory.unified_query as pkg
    assert pkg._ticket_brief("ONE-1") is None
    assert pkg._ticket_local("ONE-1") is None
    assert pkg._mcp_call("ticket_brief", {"id": "ONE-1"}) is None
