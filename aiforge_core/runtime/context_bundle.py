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
    repo_notes_md: str = ""          # <repo>/.aiforge/REPO_NOTES.md (structural map)
    repo_map_md: str = ""
    memory_md: str = ""
    ambiguous_rules_note: str = ""   # carried inside rules_md already; reserved
    used_skills: list = field(default_factory=list)
    used_workflows: list = field(default_factory=list)

    def blocks(self) -> list[str]:
        """Non-empty blocks in inject order (preferences + rules + project brief
        highest — the project brief IS the consolidated repo memory). REPO_NOTES
        (structural map) sits with the repo summary, above the raw repo map."""
        return [b for b in (self.preferences_md, self.rules_md,
                            self.project_brief_md, self.skills_md,
                            self.workflows_md, self.repo_summary_md,
                            self.repo_notes_md, self.repo_map_md,
                            self.memory_md) if b]


def _linked_brief_keys(d: dict | None) -> list[str]:
    """The sibling-brief keys a brief LINKS to (map_scopes cross-scope links) —
    ``[data-sync](compacted-data-sync.md)`` → ``data-sync``. So a recalled
    project brief pulls in the topic/global briefs it relates to (audit R5/R4)."""
    if not d:
        return []
    from aiforge_core.runtime import work_notes
    links = work_notes.parse_note(d.get("body") or "")["sections"].get("links") or []
    out: list[str] = []
    for lk in links:
        m = work_notes._BRIEF_REF_RE.match(str(lk).strip())
        if m:
            key = m.group("file")[len("compacted-"):-len(".md")]
            if key:
                out.append(key)
    return out


_BRIEF_MAX_LINKED = 3        # cap linked-brief blocks (AIFORGE_OKR_BRIEF_MAX_LINKED)
_BRIEF_TOTAL_CAP = 12000     # hard ceiling on the assembled brief text


def _dedup_lines(text: str, seen: set[str]) -> str:
    """Drop bullet lines whose normalized form was already emitted in an earlier
    block, so the SAME fact doesn't appear in project ∪ linked ∪ global."""
    out: list[str] = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s.startswith(("- ", "* ")):
            key = " ".join(s[2:].lower().split())
            if key in seen:
                continue
            seen.add(key)
        out.append(ln)
    return "\n".join(out).strip()


def _max_linked() -> int:
    """How many cross-scope links to follow. Bounded so a densely linked axis
    cannot balloon the window."""
    import os
    try:
        return max(0, int(os.environ.get("AIFORGE_OKR_BRIEF_MAX_LINKED",
                                         str(_BRIEF_MAX_LINKED))))
    except (TypeError, ValueError):
        return _BRIEF_MAX_LINKED


def _project_parts(repo: str, slug: str, seen_keys: set, seen_lines: set) -> list:
    """This repo's brief, plus the sibling briefs it LINKS to.

    Mutates ``seen_keys``/``seen_lines`` — the dedup has to span the whole
    union, not each part, or a fact repeated across linked briefs is emitted
    once per brief.
    """
    from aiforge_core.memory import md_store
    seen_keys.add(slug)
    out: list[str] = []
    d = md_store.read_file(f"compacted-{slug}")
    knowledge = _dedup_lines(_brief_knowledge(d), seen_lines)
    if knowledge:
        out.append("PROJECT MEMORY (" + repo + "):\n" + knowledge[:6000])
    # R5/R4: follow this brief's cross-scope links (bounded count).
    for lk in _linked_brief_keys(d)[:_max_linked()]:
        if lk in seen_keys:
            continue
        seen_keys.add(lk)
        lk_know = _dedup_lines(
            _brief_knowledge(md_store.read_file(f"compacted-{lk}")), seen_lines)
        if lk_know:
            out.append(f"LINKED MEMORY ({lk}):\n" + lk_know[:2000])
    return out


def _clamp_brief(out: str) -> str:
    """Hard ceiling, cut on a line boundary — never mid-fact."""
    if len(out) > _BRIEF_TOTAL_CAP:
        return out[:_BRIEF_TOTAL_CAP].rsplit("\n", 1)[0]
    return out


def project_brief_text(repo: str) -> str:
    """The compacted brief knowledge for ``repo`` — its ``compacted-<repo>.md``
    UNIONED with the sibling briefs it LINKS to (map_scopes topic/global cross-
    scope links) and the GLOBAL ``compacted-shared.md``. Mirrors the recall union
    so chat AND the pipeline see the same consolidated OKR memory. Empty until
    the axis has compacted once. Bounded: linked-brief COUNT capped, facts
    deduped across the parts, and the whole thing clamped to a hard ceiling so it
    can never balloon the window (esp. on the pipeline path, which has no other
    size guard)."""
    from aiforge_core.memory import md_store
    seen_keys: set[str] = {"shared"}      # brief keys already loaded
    seen_lines: set[str] = set()          # normalized fact lines already emitted
    parts: list[str] = []
    slug = md_store._slug(repo) if repo else ""
    if slug and slug != "shared":
        parts += _project_parts(repo, slug, seen_keys, seen_lines)
    # Global compacted brief — unioned into EVERY context.
    gk = _dedup_lines(_brief_knowledge(md_store.read_file("compacted-shared")),
                      seen_lines)
    if gk:
        parts.append("GLOBAL MEMORY:\n" + gk[:3000])
    return _clamp_brief("\n\n".join(parts))


def _project_brief(cwd: str) -> str:
    """Chat-side wrapper: resolve the repo from ``cwd`` then assemble the brief
    (project ∪ linked ∪ global) via :func:`project_brief_text`."""
    from aiforge_core.runtime import repo_ident
    return project_brief_text(repo_ident.repo_name(cwd, sentinel=""))


def _repo_notes(cwd: str) -> str:
    """Load ``<repo>/.aiforge/REPO_NOTES.md`` for ``cwd`` (structural repo map:
    controllers, services, event surface, cross-repo contracts) and return its
    KNOWLEDGE for injection — the OKR envelope's metadata (Objective, title,
    sentinel) is stripped via ``work_notes.knowledge_text`` so only the actual
    structure reaches the window. Empty when there's no notes file.

    Looks in ``cwd`` and its git top-level (a chat/ticket cwd is usually the
    worktree; the notes file lives at the repo root)."""
    import os
    cands: list[str] = []
    if cwd:
        cands.append(os.path.join(cwd, ".aiforge", "REPO_NOTES.md"))
    try:
        import subprocess
        top = subprocess.run(
            ["git", "-C", cwd or ".", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        if top:
            cands.append(os.path.join(top, ".aiforge", "REPO_NOTES.md"))
    except Exception:  # noqa: BLE001
        pass
    for path in cands:
        try:
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            continue
        from aiforge_core.runtime import work_notes
        know = work_notes.knowledge_text(raw).strip()
        if know:
            return "REPO STRUCTURE (from REPO_NOTES.md):\n" + know[:5000]
    return ""


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


def _fill_priority(b, cwd, query, _ca, *, want_prefs: bool, want_rules: bool) -> None:
    """Standing preferences, the rule book, and the compacted project brief.

    Highest priority — these survive a tight window, so they are gathered
    first and separately from the optional blocks.
    """
    if want_prefs:
        b.preferences_md = _safe(lambda: _ca._preferences_context(cwd))
    if want_rules:
        b.rules_md = _safe(lambda: _ca._rules_context(cwd, query))
    # Consolidated per-repo project memory (compacted brief) — load it whenever
    # we have a repo, so opening a project brings its accumulated memory.
    b.project_brief_md = _safe(lambda: _project_brief(cwd))


def _fill_playbooks(b, cwd, query, ctx_on, _sk, _wf) -> None:
    """Relevance-matched workflows and skills.

    Both are static QUALITY context (how to do the task right), NOT the growing
    history that makes small models drift — so BOTH are built even in cave
    mode. Dropping skills to save tokens was a quality regression; token safety
    comes from condensing HISTORY early plus the system-prompt cap, which trims
    the lowest-priority TAIL first (skills sit above the repo map, so they
    survive a tight window).
    """
    if ctx_on("workflows"):
        b.workflows_md = _safe(lambda: _wf.auto_context(query, cwd))
        b.used_workflows = _safe(lambda: _wf.selected_names(query, cwd), default=[])
    if ctx_on("skills"):
        b.skills_md = _safe(lambda: _sk.auto_context(query, cwd))
        b.used_skills = _safe(lambda: _sk.selected_names(query, cwd), default=[])


def _fill_repo_context(b, cwd, ctx_on, _ca, *, cave: bool,
                       want_summary: bool, want_repo_map: bool) -> None:
    """Repo summary, the structural REPO_NOTES map, and the repo map itself —
    all of it cheaper and smaller in cave mode."""
    if want_summary and ctx_on("summary"):
        b.repo_summary_md = _safe(lambda: _ca._repo_context(cwd))
        # Structural repo map (REPO_NOTES.md) — deterministic controllers/
        # services/event-surface reference; loaded with the summary, cheap
        # (single file read) and skipped in cave mode with the rest of summary.
        if not cave:
            b.repo_notes_md = _safe(lambda: _repo_notes(cwd))
    if want_repo_map and ctx_on("repomap"):
        b.repo_map_md = _safe(lambda: _ca._build_repo_map(
            cwd, max_entries=(60 if cave else 160), max_depth=(2 if cave else 3)))


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
    _fill_priority(b, cwd, query, _ca, want_prefs=want_prefs, want_rules=want_rules)
    _fill_playbooks(b, cwd, query, ctx_on, _sk, _wf)
    _fill_repo_context(b, cwd, ctx_on, _ca, cave=cave,
                       want_summary=want_summary, want_repo_map=want_repo_map)
    if ctx_on("recall"):
        b.memory_md = _safe(lambda: _ca._memory_recall(
            cwd, query, limit=(3 if cave else 6), session_id=session_id))
    return b


__all__ = ["ContextBundle", "build_bundle"]
