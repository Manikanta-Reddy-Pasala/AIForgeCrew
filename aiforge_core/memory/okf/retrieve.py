"""Surgical retrieval + prompt compilation over the OKR DAG.

Given the active Key Result, gather ONLY what the model needs — not the whole
graph:

  * ascend  → the parent Objective (title + short context) = the *why*
  * descend → the active KR (full body)                    = the *what*
  * how     → Learnings scoped global | to the objective   = the *constraints*
  * when    → the last N sessions covering the KR          = *recent activity*

then :func:`compile_prompt` stitches it into a bounded ``<OBJECTIVE>/…`` block
for the system prompt (no Jinja2 dependency — a plain, capped template).
"""
from __future__ import annotations

import os

from . import graph as _graph


def _cap(v: str, n: int) -> str:
    v = (v or "").strip()
    return v if len(v) <= n else v[:n].rstrip() + " …"


def _first_para(body: str, n: int) -> str:
    """The objective's context = the first meaningful paragraph, capped (saves
    tokens vs the whole body when we only need the 'why')."""
    for block in (body or "").split("\n\n"):
        b = block.strip()
        if b and not b.startswith("#"):
            return _cap(b, n)
    return _cap((body or "").lstrip("#").strip(), n)


def _kr_view(kr: dict, kr_id: str, kr_chars: int) -> dict:
    m = kr.get("meta") or {}
    return {"id": kr_id, "title": m.get("title") or kr_id,
            "status": m.get("status"), "metrics": m.get("metrics"),
            "body": _cap(kr.get("body") or "", kr_chars)}


def _objective_view(o: dict, o_id: str, obj_ctx_chars: int) -> dict:
    m = o.get("meta") or {}
    return {"id": o_id, "title": m.get("title") or o_id,
            "status": m.get("status"),
            "context": _first_para(o.get("body") or "", obj_ctx_chars)}


def _learning_views(g, o_id, learn_chars: int) -> list[dict]:
    out = []
    for lid in g.learnings_for(o_id):
        ln = g.get(lid)
        if ln:
            out.append({"id": lid,
                        "category": (ln.get("meta") or {}).get("category"),
                        "rule": _cap(ln.get("body") or "", learn_chars)})
    return out


def _session_views(g, kr_id, recent_sessions: int, session_chars: int) -> list[dict]:
    out = []
    for sid in g.sessions_of(kr_id, limit=max(0, recent_sessions)):
        s = g.get(sid)
        if s:
            out.append({"id": sid,
                        "date": (s.get("meta") or {}).get("date") or sid,
                        "log": _cap(s.get("body") or "", session_chars)})
    return out


def retrieve(kr_id: str | None = None, *, recent_sessions: int = 2,
             graph: "_graph.Graph | None" = None,
             obj_ctx_chars: int = 600, kr_chars: int = 1600,
             learn_chars: int = 400, session_chars: int = 500) -> dict:
    """Assemble the surgical context for ``kr_id`` (defaults to the active KR).
    Returns a dict the compiler renders; empty pieces are simply omitted."""
    g = graph or _graph.build()
    kr_id = kr_id or _graph.get_active()
    out: dict = {"objective": None, "active_kr": None,
                 "learnings": [], "sessions": []}
    if not kr_id:
        return out
    kr = g.get(kr_id)
    if kr:
        out["active_kr"] = _kr_view(kr, kr_id, kr_chars)
    o_id = g.objective_of(kr_id)
    o = g.get(o_id) if o_id else None
    if o:
        out["objective"] = _objective_view(o, o_id, obj_ctx_chars)
    out["learnings"] = _learning_views(g, o_id, learn_chars)
    out["sessions"] = _session_views(g, kr_id, recent_sessions, session_chars)
    return out


def _objective_block(o) -> str:
    """Render the OBJECTIVE section (title + optional context)."""
    return (f"<OBJECTIVE id=\"{o['id']}\">\n{o['title']}"
            + (f"\n{o['context']}" if o.get("context") else "")
            + "\n</OBJECTIVE>")


def _active_task_block(kr) -> str:
    """Render the ACTIVE_TASK section (id/title + optional status/metrics/body)."""
    head = f"{kr['id']} · {kr['title']}"
    if kr.get("status"):
        head += f" [{kr['status']}]"
    met = f"\nMetrics: {kr['metrics']}" if kr.get("metrics") else ""
    body = f"\n{kr['body']}" if kr.get("body") else ""
    return f"<ACTIVE_TASK>\n{head}{met}{body}\n</ACTIVE_TASK>"


def compile_prompt(ctx: dict) -> str:
    """Render the retrieved context into a compact system-prompt block. Empty
    sections are dropped, so a bare graph yields an empty string."""
    if not ctx or not (ctx.get("objective") or ctx.get("active_kr")):
        return ""
    parts: list[str] = []
    o = ctx.get("objective")
    if o:
        parts.append(_objective_block(o))
    kr = ctx.get("active_kr")
    if kr:
        parts.append(_active_task_block(kr))
    learn = ctx.get("learnings") or []
    if learn:
        lines = "\n".join(f"- {l['rule']}" for l in learn)
        parts.append(f"<CRITICAL_RULES>\n{lines}\n</CRITICAL_RULES>")
    sess = ctx.get("sessions") or []
    if sess:
        blocks = "\n\n".join(f"{s['date']} ({s['id']}):\n{s['log']}" for s in sess)
        parts.append(f"<RECENT_ACTIVITY>\n{blocks}\n</RECENT_ACTIVITY>")
    return "\n\n".join(parts)


def _tokens(text: str) -> set:
    import re
    return set(re.findall(r"[a-z0-9]{4,}", (text or "").lower()))


def _node_tokens(d: dict) -> set:
    m = d.get("meta") or {}
    return _tokens(str(m.get("title") or "") + " " + str(m.get("category") or "")
                   + " " + (d.get("body") or ""))


def _fuzzy_score(q: set, nt: set) -> float:
    """Typo/stem-tolerant overlap between query tokens ``q`` and node tokens
    ``nt``: a shared 5-char prefix (evict/eviction/evicting) or an edit-distance
    ratio ≥ 0.8 (evicton→eviction) counts. Used only when EXACT overlap is 0."""
    import difflib
    total = 0.0
    for qt in q:
        best = 0.0
        for t in nt:
            if len(qt) >= 5 and len(t) >= 5 and (t[:5] == qt[:5]):
                best = max(best, 0.9)
            else:
                r = difflib.SequenceMatcher(None, qt, t).ratio()
                if r >= 0.8:
                    best = max(best, r)
        total += best
    return total


def _rank_by_query(nodes: list, query: str, top_k: int,
                   recent_key=None, *, require_match: bool = False) -> list:
    """Return the ``top_k`` nodes most RELEVANT to ``query`` via a fallback
    LADDER: (1) EXACT token overlap; (2) if nothing matched exactly, a FUZZY
    pass (shared prefix / edit-distance, so a typo or a stem still finds the
    note); (3) if still nothing, recency (or given) order — context is never
    empty. This is what makes read return RELATED documents per task even when
    the wording doesn't match verbatim, not the whole scope."""
    if not nodes:
        return []
    q = _tokens(query)
    if q:
        # tier 1 — exact
        exact = sorted(nodes, key=lambda d: len(q & _node_tokens(d)), reverse=True)
        if len(q & _node_tokens(exact[0])) > 0:
            return [d for d in exact if len(q & _node_tokens(d)) > 0][:top_k]
        # tier 2 — fuzzy (only when NO exact hit anywhere)
        fuzzy = sorted(nodes, key=lambda d: _fuzzy_score(q, _node_tokens(d)),
                       reverse=True)
        if _fuzzy_score(q, _node_tokens(fuzzy[0])) > 0:
            return [d for d in fuzzy if _fuzzy_score(q, _node_tokens(d)) > 0][:top_k]
    # tier 3 — no query / nothing matched → recency (or original) order.
    # ``require_match`` opts OUT of this: "context is never empty" is right for
    # a repo card the agent needs regardless, and wrong for MANDATORY rules.
    # Without it, a query matching no global rule still got the most recent
    # eight injected as orders — which is how `calc.py` conventions ended up in
    # front of a Jira question. For rules, nothing relevant means inject nothing.
    if require_match and query.strip():
        return []
    if recent_key:
        nodes = sorted(nodes, key=recent_key, reverse=True)
    return nodes[:top_k]


def _shared(local: list[dict], query: str, max_view: int) -> tuple[list, list]:
    """``(local minus what the view already states, the view nodes to render)``.

    ``okf.tiers`` owns which directory the view is, what counts as usable content
    in it and what "already stated" means; this only ranks. Soft-fail: an
    unreadable view degrades recall to purely local knowledge rather than losing
    the prompt.
    """
    try:
        from . import tiers

        view = _rank_by_query(tiers.view_nodes(), query, max_view)
        # Drop the locally-authored notes the fold already carries, so a fact
        # held both in okf/ and in view/ is rendered exactly once.
        return (tiers.unrepresented(local, view) if view else local), view
    except Exception:  # noqa: BLE001 — a bad view must never break recall
        return local, []


def _shared_entry(d: dict, chars: int) -> str:
    m = d.get("meta") or {}
    head = str(m.get("title") or d.get("id") or "").strip()
    return (f"## {head}\n" if head else "") + _cap(d.get("body") or "", chars)


def _node_line(d: dict) -> str:
    m = d.get("meta") or {}
    cat = m.get("category") or m.get("topic")
    head = (m.get("title") or (d.get("body") or "").strip().split("\n", 1)[0])
    return f"- {('[' + cat + '] ') if cat else ''}{_cap(head, 160)}"


def _recency_key(d):
    return ((d.get("meta") or {}).get("timestamp") or "", d.get("id") or "")


def _script_line(d: dict) -> str:
    m = d.get("meta") or {}
    purpose = m.get("purpose")
    return (f"- {m.get('name')} [{m.get('lang')}]"
            + (f" — {purpose}" if purpose else ""))


def _global_parts(store, query: str, max_global: int, max_view: int,
                  view_chars: int) -> list[str]:
    """The global rules + the mesh's distilled knowledge.

    ``view/`` is read here and nowhere else — it is the peer-to-peer feature's
    only consumer, so without it knowledge replicates, folds and reaches no
    agent. ``mesh/`` and ``peers/`` stay unread: they are inputs to the fold,
    and the local nodes the fold already carries are dropped so one fact cannot
    arrive twice.
    """
    parts: list[str] = []
    gl = [d for d in store.load_all("global") if d.get("type") == "learning"]
    gl, shared = _shared(gl, query, max_view)
    gl = _rank_by_query(gl, query, max_global, require_match=True)
    if gl:
        parts.append("<GLOBAL_RULES>\n"
                     + "\n".join(_node_line(d) for d in gl) + "\n</GLOBAL_RULES>")
    if shared:
        parts.append("<SHARED_KNOWLEDGE>\n"
                     + "\n\n".join(_shared_entry(d, view_chars) for d in shared)
                     + "\n</SHARED_KNOWLEDGE>")
    return parts


def _project_body(proj: list, query: str, max_repo_learn: int,
                  max_repo_sol: int) -> list[str]:
    """The repo's own memory: its CARD first (the hub — how to build/test/run,
    structure), then scripts, task recipes, learnings and solved work."""
    ranked = {
        "Scripts": (_rank_by_query([d for d in proj if d.get("type") == "script"],
                                   query, 6, recent_key=_recency_key),
                    _script_line),
        "Task recipes": (_rank_by_query([d for d in proj if d.get("type") == "task"],
                                        query, 6, recent_key=_recency_key),
                         _node_line),
        "Learnings": (_rank_by_query(
            [d for d in proj if d.get("type") == "learning"], query,
            max_repo_learn, recent_key=_recency_key), _node_line),
        "Recently solved": (_rank_by_query(
            [d for d in proj if d.get("type") == "solution"], query,
            max_repo_sol, recent_key=_recency_key), _node_line),
    }
    body: list[str] = []
    card = next((d for d in proj if d.get("type") == "repo"), None)
    if card:
        body.append("Profile:\n" + _repo_card(card))
    for heading, (items, render) in ranked.items():
        if items:
            body.append(f"{heading}:\n"
                        + "\n".join(render(d) for d in items))
    return body


def _scoped_block(repo: str | None, *, query: str = "", max_global: int = 8,
                  max_repo_learn: int = 10, max_repo_sol: int = 6,
                  max_view: int = 3, view_chars: int = 1200) -> str:
    """SCOPE- AND QUERY-aware memory: the global rules + the mesh's distilled
    knowledge + THIS repo's learnings and solutions that are most RELEVANT to
    ``query`` — not every document in the scope, and nothing from OTHER projects.
    Scope stops cross-project leakage; the query ranking stops dumping the whole
    bundle when only a few notes matter.
    """
    try:
        from . import store
    except Exception:  # noqa: BLE001
        return ""
    parts = _global_parts(store, query, max_global, max_view, view_chars)
    if repo:
        body = _project_body(store.load_all(repo), query, max_repo_learn,
                             max_repo_sol)
        if body:
            parts.append(f"<PROJECT_MEMORY repo=\"{repo}\">\n"
                         + "\n\n".join(body) + "\n</PROJECT_MEMORY>")
    return "\n\n".join(parts)


def _repo_card(d: dict) -> str:
    """Compact render of a repo hub card — the build/test/run/structure lines
    that matter, then a couple of gotchas."""
    m = d.get("meta") or {}
    lines = []
    for k in ("stack", "build", "test", "run", "structure", "deploy"):
        v = m.get(k)
        if v:
            lines.append(f"  {k}: {_cap(str(v), 160)}")
    for k in ("services", "tables", "scripts", "workflows"):
        v = m.get(k)
        if v:
            lines.append(f"  {k}: {', '.join(str(x) for x in v[:8])}")
    goch = m.get("gotchas") or []
    if goch:
        lines.append("  gotchas: " + "; ".join(str(g) for g in goch[:4]))
    return "\n".join(lines) or "  (empty)"


def context_block(kr_id: str | None = None, *, repo: str | None = None,
                  query: str = "", **kw) -> str:
    """One-shot: the (active) KR's goal context PLUS scope- and query-aware
    memory — the global rules + THIS ``repo``'s learnings/solutions most RELEVANT
    to ``query``, never other projects' and never the whole bundle. This is what
    the context bundle injects. Never raises — returns '' on any error."""
    try:
        base = compile_prompt(retrieve(kr_id, **kw))
    except Exception:  # noqa: BLE001
        base = ""
    # Only surface scoped memory when there's a context signal (a repo or a
    # query) to attach it to — a bare call with neither stays empty rather than
    # dumping the global rules into a contextless prompt.
    scoped = _scoped_block(repo, query=query) if (repo or query) else ""
    return "\n\n".join(x for x in (base, scoped) if x)


__all__ = ["retrieve", "compile_prompt", "context_block"]
