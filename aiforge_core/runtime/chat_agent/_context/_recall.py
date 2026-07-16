from __future__ import annotations

import os
import re

from .._shell import _workspace_root
from .._tools import _cached_find_by_source, _chat_repo_key
from ._repomap import _repo_name


# WEB-lookup intent. STRONG cues explicitly ask for the open web → always force
# web_search. WEAK cues ("latest version", "release notes") also mean the web —
# BUT commonly appear in LOCAL-code questions too ("bump to the latest version in
# package.json", "what's the current version in my config"), so they only fire
# when NO local-code indicator is present. A bare URL is in neither list — a URL
# already routes to web_crawl/web_fetch.
_WEB_INTENT_STRONG_RE = re.compile(
    r"\b(search\s+(the\s+)?web|search\s+online|web\s+search|google\s+(it|for)|"
    r"look\s+(it\s+)?up\s+(online|on\s+the\s+web)|on\s+the\s+internet|"
    r"what'?s\s+new\s+in|recent\s+news|as\s+of\s+(today|now))\b",
    re.IGNORECASE)
_WEB_INTENT_WEAK_RE = re.compile(
    r"\b(latest\s+(version|release|news|stable)|current\s+version|"
    r"newest\s+version|release\s+notes|up[-\s]?to[-\s]?date)\b",
    re.IGNORECASE)
# Signals the "latest/current version" question is about THIS codebase, not the
# web — suppress the weak web cue then.
_LOCAL_CODE_CTX_RE = re.compile(
    r"(`[^`]+`|\b[\w./-]+\.(py|js|ts|tsx|jsx|java|go|rs|rb|json|ya?ml|toml|txt|md|"
    r"cfg|ini|lock|xml|gradle)\b|package\.json|requirements\.txt|pyproject|"
    r"\bmy\s+(code|repo|project|config|file|app)|\bthis\s+(repo|project|file|"
    r"codebase|code)\b|\bin\s+(the|my|this)\s+\w+)",
    re.IGNORECASE)


def _has_web_intent(text: str) -> bool:
    """True when the user's message signals a LIVE-WEB lookup and carries no URL
    (a URL already drives web_crawl). Strong cues always match; weak version/
    release cues are suppressed when the message is clearly about local code —
    so "bump to the latest version in package.json" does NOT force a web search."""
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"https?://", t):   # a URL → web_crawl path already handles it
        return False
    if _WEB_INTENT_STRONG_RE.search(t):
        return True
    if _WEB_INTENT_WEAK_RE.search(t) and not _LOCAL_CODE_CTX_RE.search(t):
        return True
    return False


_WEB_LOOKUP_DIRECTIVE = (
    "[web lookup required] The user is asking for information that must come "
    "from the LIVE web (a search, the latest/current version, release notes, "
    "or recent news). You MUST call `web_search` FIRST with a focused query, "
    "then `web_fetch` the most authoritative result to confirm, and base your "
    "answer ONLY on what you find — do NOT answer from prior knowledge, it may "
    "be out of date. If web_search returns no results, refine the query (drop "
    "years/qualifiers) and retry ONCE before saying you couldn't find it."
)


# Keyword → tool-scope tag: which tool a request is likely to use. A recalled
# learning tagged ``tool:<name>`` (see the learner guidance) gets a score bump
# in recall so the working JQL/filter/config the agent figured out LAST time
# resurfaces for the same TYPE of request — instead of re-deriving it.
_TOOL_TAG_HINTS = {
    "tool:jira": ("jira", "jql", "issue", "ticket", "sprint", "epic"),
    "tool:confluence": ("confluence", "wiki", "space", "page"),
    "tool:git": ("git", "branch", "commit", "rebase", "pull request", " pr ", "merge"),
    "tool:email": ("email", "smtp", "inbox", "mailbox"),
    "tool:gitlab": ("gitlab", "merge request", " mr "),
}


def _tool_tags(query: str) -> list[str]:
    q = f" {(query or '').lower()} "
    return [tag for tag, kws in _TOOL_TAG_HINTS.items()
            if any(k in q for k in kws)]


_ASK_LEAD_RE = re.compile(
    r"^(?:also|and|plus|then|next|additionally|why|how|what|when|where|which|"
    r"who|can|could|should|would|is|are|does|do|did|will|fix|add|make|check|"
    r"recheck|verify|update|create|remove|delete|use|show|explain|list|"
    r"implement|write|run|test|deploy|review|rename|refactor|change|ensure)\b",
    re.IGNORECASE)


def _split_asks(text: str, cap: int = 8) -> list[str]:
    """Break the user's CURRENT message into its distinct asks so a
    multi-part message ("fix X. also why does Y happen? and add Z") gets a
    CHECKLIST instead of the model answering part 1 and stopping — simple
    mode has no enhancer/spec, so nothing else tracks the parts. Heuristic
    and conservative: bullets/numbered lines count as-is; otherwise sentence
    segments that look like a question or an imperative. Returns [] (no
    checklist) when only one ask is found."""
    t = (text or "").strip()
    if len(t) < 25:
        return []
    parts: list[str] = []
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    bullets = [re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", ln) for ln in lines
               if re.match(r"^(?:[-*•]|\d+[.)])\s+", ln)]
    if len(bullets) >= 2:
        parts = bullets
    else:
        # sentence segmentation + " also "/" and then " connectors
        segs: list[str] = []
        for chunk in re.split(r"(?<=[?.!;])\s+|\n+", t):
            segs.extend(re.split(
                r"\s+(?=(?:also|and then|and also|plus|additionally)\b)",
                chunk, flags=re.IGNORECASE))
        for s in segs:
            s = s.strip(" .")
            if len(s) < 12:
                continue
            if s.endswith("?") or _ASK_LEAD_RE.match(s):
                parts.append(s)
    parts = [p[:160] for p in parts if p.strip()][:cap]
    return parts if len(parts) >= 2 else []


def _memory_recall(cwd: str, query: str, limit: int = 6,
                   session_id: "int | None" = None) -> str:
    """Proactive memory recall at SESSION START — pull prior decisions /
    gotchas / learnings relevant to the user's opening request so the agent
    arrives informed (self-learning) instead of re-deriving what past
    sessions already worked out. Best-effort: never breaks the turn."""
    q = (query or "").strip()
    if not q:
        return ""
    hits: list[dict] = []
    try:
        from aiforge_core.memory import unified_query as _uq
        # F2/M3: recall under the SAME repo the chat WRITE path files facts
        # under (git-toplevel basename), else sqlite_memory.recall filters
        # them out (WHERE repo=?). M4: exclude the current live session so
        # this turn's own messages don't return as "prior chat".
        _repo = _chat_repo_key(cwd)
        res = _uq.query(q, limit=limit, repo=_repo,
                        exclude_session=session_id,
                        boost_tags=_tool_tags(q))
        if isinstance(res, dict):
            hits = res.get("hits", []) or []
    except Exception:  # noqa: BLE001
        hits = []
    if not hits:
        return ""
    _preamble = ("RELEVANT MEMORY recalled for this request (prior decisions / "
                 "gotchas / learnings from earlier sessions — consult before "
                 "re-deriving):\n")
    # Map→summarize: many scattered hits → ONE compact briefing (LLM). Empty
    # (disabled / too few / model down) falls back to the raw ranked list.
    try:
        from aiforge_core.memory import recall_summary
        brief = recall_summary.summarize_hits(q, hits)
    except Exception:  # noqa: BLE001
        brief = ""
    if brief:
        return _preamble + brief
    lines: list[str] = []
    for h in hits:
        txt = (h.get("text") or "").strip().replace("\n", " ")
        if not txt:
            continue
        src = h.get("source") or ""
        lines.append(f"- {txt[:240]}" + (f"  ({src})" if src else ""))
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return _preamble + "\n".join(lines)


def _chat_session_recall(query: str, session_id: "int | None",
                         limit: int = 4, drop_session: "int | None" = None) -> str:
    """Proactive recall from PRIOR CHAT SESSIONS — surface things the user
    discussed in OTHER conversations that may bear on this request, so simple
    chat has continuity across sessions (not just within one). Cheap + local
    (one SQLite scan). Best-effort: never breaks the turn."""
    q = (query or "").strip()
    if not q:
        return ""
    try:
        from aiforge_core.runtime import chat_store
        hits = chat_store.search_messages(q, limit=limit + 2,
                                          exclude_session=session_id)
    except Exception:  # noqa: BLE001
        hits = []
    lines: list[str] = []
    for h in hits:
        # the immediate-prior session is already injected as prev-session — skip
        # its hits here so it doesn't double-surface (older sessions still show).
        if drop_session is not None and h.get("session_id") == drop_session:
            continue
        content = (h.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        if len(lines) >= limit:
            break
        title = h.get("session_title") or "chat"
        role = h.get("role") or "user"
        lines.append(f"- [{title}] {role}: {content}")
    if not lines:
        return ""
    return ("RELEVANT PRIOR CHAT SESSIONS — things you discussed with the user "
            "in OTHER conversations that may bear on this request (cite them if "
            "you use them):\n" + "\n".join(lines))


def _repo_context(cwd: str) -> str:
    """The persistent PROJECT SUMMARY for this repo — what it is + what's
    been done — injected every turn so follow-ups have continuity. Read
    from the per-repo memory file (source=repo:<name>); if none exists yet,
    auto-build a starter from the detected stack + README so there's always
    something. The summary is updated at the end of each session run."""
    base = str(_workspace_root() or cwd)
    repo = _repo_name(cwd)
    try:
        from aiforge_core.memory import md_store
        p = _cached_find_by_source(f"repo:{repo}")
        if p is not None:
            body = md_store._parse(p).get("body", "")
            if body.strip():
                return (f"PROJECT SUMMARY — {repo} (what this repo is + what "
                        f"prior sessions did):\n{body[:1800]}")
    except Exception:  # noqa: BLE001
        pass
    # Starter (first time): stack + README excerpt.
    stacks: list[str] = []
    try:
        from aiforge_core.runtime.tools.project_runner import detect
        stacks = detect(base).get("stacks", [])
    except Exception:  # noqa: BLE001
        pass
    readme = ""
    for rn in ("README.md", "Readme.md", "readme.md", "README.rst", "README.txt"):
        rp = os.path.join(base, rn)
        if os.path.isfile(rp):
            try:
                readme = open(rp, encoding="utf-8", errors="ignore").read()[:700]
            except Exception:  # noqa: BLE001
                pass
            break
    out = f"PROJECT SUMMARY — {repo} (auto-detected; refine as you learn):\n"
    out += f"- Stack(s): {', '.join(stacks) or 'unknown'}\n"
    if readme:
        out += f"- README excerpt:\n{readme}\n"
    return out
