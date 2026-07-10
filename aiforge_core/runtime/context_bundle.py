"""ONE context-bundle builder — the single source of *which* context sources
get gathered, *how* they're scoped (cwd/repo always threaded), and *how* they're
query-gated.

Four execution paths (single chat, chat-team, ticket pipeline, parallel
subtasks) previously each assembled context ad-hoc, calling the same leaf
helpers with inconsistent args (query passed or not, cwd passed or not, which
rule store, which pref store). That inconsistency WAS the recurring
"works-in-chat-not-in-team" / precedence drift. This module wraps the existing
leaf helpers (it does NOT reimplement them) so every path gets the same policy.

Fields are pre-rendered markdown strings ("" when the source is off/empty).
Callers decide how to place them (chat: system blocks; pipeline: ADK state keys).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContextBundle:
    preferences_md: str = ""
    rules_md: str = ""
    project_brief_md: str = ""       # the loaded per-repo "project memory"
    skills_md: str = ""
    workflows_md: str = ""
    repo_summary_md: str = ""
    repo_map_md: str = ""
    memory_md: str = ""
    ambiguous_rules_note: str = ""   # carried inside rules_md already; reserved
    used_skills: list = field(default_factory=list)
    used_workflows: list = field(default_factory=list)

    def blocks(self) -> list[str]:
        """Non-empty blocks in inject order (preferences + rules + project brief
        highest — the project brief IS the consolidated repo memory)."""
        return [b for b in (self.preferences_md, self.rules_md,
                            self.project_brief_md, self.skills_md,
                            self.workflows_md, self.repo_summary_md,
                            self.repo_map_md, self.memory_md) if b]


def _project_brief(cwd: str) -> str:
    """The compacted project brief for this context — the per-scope
    ``compacted-<repo>.md`` (a repo, or a Jira ticket / Confluence page whose
    folder is the cwd) UNIONED with the GLOBAL ``compacted-shared.md`` (general
    knowledge). Both are products of the repo-axis compaction; loading both here
    mirrors the recall union so a ticket chat sees its own compacted memory AND
    global. Empty until the axis has compacted at least once; capped per part so
    neither dominates the window."""
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import repo_ident
    repo = repo_ident.repo_name(cwd, sentinel="")
    parts: list[str] = []
    if repo and md_store._slug(repo) != "shared":
        knowledge = _brief_knowledge(
            md_store.read_file(f"compacted-{md_store._slug(repo)}"))
        if knowledge:
            parts.append("PROJECT MEMORY (" + repo + "):\n" + knowledge[:6000])
    # Global compacted brief — unioned into EVERY context (skip when we ARE it).
    gk = _brief_knowledge(md_store.read_file("compacted-shared"))
    if gk:
        parts.append("GLOBAL MEMORY:\n" + gk[:3000])
    return "\n\n".join(parts)


def _brief_knowledge(d: dict | None) -> str:
    """The injectable KNOWLEDGE of an OKR memory brief — Facts + consolidated
    body, minus the envelope metadata (Objective boilerplate, title, sentinel).
    Delegates to the shared ``work_notes.knowledge_text`` so injection and
    recall-ingest strip identically. Legacy briefs degrade to their raw body."""
    body = ((d or {}).get("body") or "").strip()
    if not body:
        return ""
    try:
        from aiforge_core.runtime import work_notes
        return work_notes.knowledge_text(body)   # read_file already stripped fm
    except Exception:  # noqa: BLE001
        return body


def _safe(fn, default=""):
    try:
        return fn() or default
    except Exception:  # noqa: BLE001 — one bad source never breaks the bundle
        return default


def build_bundle(cwd: str, query: str, *, cave: bool = False,
                 ctx_on=None, session_id=None, want_repo_map: bool = True,
                 want_summary: bool = True, want_rules: bool = True,
                 want_prefs: bool = True) -> ContextBundle:
    """Gather the full context bundle for ``query`` in ``cwd``. Reuses the
    existing chat_agent / skills / workflows helpers (lazy-imported to avoid a
    circular import). ``ctx_on(block)`` toggles optional blocks (defaults on).
    ``want_rules``/``want_prefs`` let a caller that already injects those
    high-priority blocks itself skip the recompute."""
    if ctx_on is None:
        ctx_on = lambda _b: True   # noqa: E731
    from aiforge_core.runtime import chat_agent as _ca
    from aiforge_core.runtime import skills as _sk
    from aiforge_core.runtime import workflows as _wf

    b = ContextBundle()
    # Standing preferences + rule book — highest priority, always on.
    if want_prefs:
        b.preferences_md = _safe(lambda: _ca._preferences_context(cwd))
    if want_rules:
        b.rules_md = _safe(lambda: _ca._rules_context(cwd, query))
    # Consolidated per-repo project memory (compacted brief) — load it whenever
    # we have a repo, so opening a project brings its accumulated memory.
    b.project_brief_md = _safe(lambda: _project_brief(cwd))
    # Relevance-matched playbooks. WORKFLOWS are built even in cave mode — a
    # matched workflow is a MANDATORY user procedure (branch/MR conventions);
    # silently dropping it on a small window made the agent skip it (e.g.
    # commit straight to main). Skills stay cave-skipped (searchable on demand).
    if ctx_on("workflows"):
        b.workflows_md = _safe(lambda: _wf.auto_context(query, cwd))
        b.used_workflows = _safe(lambda: _wf.selected_names(query, cwd), default=[])
    if not cave and ctx_on("skills"):
        b.skills_md = _safe(lambda: _sk.auto_context(query, cwd))
        b.used_skills = _safe(lambda: _sk.selected_names(query, cwd), default=[])
    if want_summary and ctx_on("summary"):
        b.repo_summary_md = _safe(lambda: _ca._repo_context(cwd))
    if want_repo_map and ctx_on("repomap"):
        b.repo_map_md = _safe(lambda: _ca._build_repo_map(
            cwd, max_entries=(60 if cave else 160), max_depth=(2 if cave else 3)))
    if ctx_on("recall"):
        b.memory_md = _safe(lambda: _ca._memory_recall(
            cwd, query, limit=(3 if cave else 6), session_id=session_id))
    return b


__all__ = ["ContextBundle", "build_bundle"]
