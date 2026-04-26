"""UnifiedContext — the ONE read API every agent calls.

Every retrieval source the project owns funnels through here. No
agent should ever read from a memory backend directly again.

Sources merged (each best-effort, soft-fail individually):

  1. unified_query  — 6-source aggregator (T2 facts, T1 episodic,
                      tickets, related symbols, doc/markdown,
                      external library docs)
  2. RepoMap        — Aider tree-sitter PageRank digest (T4/T5)
  3. graph_neighbours — Neo4j :Symbol CALLS/IMPORTS edges (T5)
  4. repo_standards — Neo4j :Repo manifest (build/test/lint/conv)
  5. similar_tickets — Postgres `tickets` text search
  6. T3 patterns    — Memory.search filtered to tier='t3'
                      (learner_facts + auto-promoted patterns)
  7. CLAUDE.md / README.md — top-of-repo doc (per-repo focal text)
  8. claude-memory  — operator's ~/.claude/memory/*.md grep

Public surface:
    bundle = UnifiedContext().for_intent(intent, role=, token_budget=)
    bundle = UnifiedContext().for_chat(text)
    bundle = UnifiedContext().for_planner(ticket)
    bundle = UnifiedContext().for_doer(ticket)

Each variant is a thin wrapper around .for_intent — they all
classify the input first (cheap LLM call cached per request) so
behaviour is uniform.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from aiforge_core.runtime.config import WORKTREE_ROOT

if TYPE_CHECKING:
    from aiforge_core.intent.classifier import Intent


# ───────── data shape ─────────────────────────────────────────────


@dataclass
class ContextBundle:
    """Render-ready output of UnifiedContext.

    All string fields are token-budget-trimmed and ready to drop
    straight into a prompt. Lists carry the raw structured data so
    callers can attach to ticket metadata or rerank further.
    """
    intent: "Intent | None" = None
    repo: str = ""
    focal_files: list[str] = field(default_factory=list)
    reference_files: list[str] = field(default_factory=list)
    repo_map_text: str = ""
    neighbours_text: str = ""
    standards_text: str = ""
    similar_tickets: list[dict] = field(default_factory=list)
    similar_tickets_text: str = ""
    t3_recipes: list[str] = field(default_factory=list)
    t3_text: str = ""
    repo_doc_text: str = ""           # CLAUDE.md / README.md tail
    operator_memory_text: str = ""    # ~/.claude/memory hits
    memory_hits_text: str = ""        # unified_query render
    commands: dict[str, str] = field(default_factory=dict)
    acceptance: list[str] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        """One big prompt-ready block. Empty sections are skipped."""
        out: list[str] = []

        def section(title: str, body: str) -> None:
            body = (body or "").strip()
            if body:
                out.append(f"## {title}\n{body}")

        if self.intent:
            out.append(
                f"## Resolved intent\n"
                f"- action: `{self.intent.action}`\n"
                f"- entity: `{self.intent.entity or '?'}`\n"
                f"- reference: `{self.intent.reference_pattern or '-'}`\n"
                f"- repo: `{self.repo or self.intent.repo_hint or '?'}`"
            )
        if self.focal_files:
            out.append(
                "## Focal files (start here)\n"
                + "\n".join(f"- {p}" for p in self.focal_files[:12])
            )
        if self.reference_files:
            out.append(
                "## Reference files (mirror this pattern)\n"
                + "\n".join(f"- {p}" for p in self.reference_files[:8])
            )
        section("Project standards", self.standards_text)
        section("Code map (Aider RepoMap)", self.repo_map_text)
        section("Neighbour symbols (Graphify + tree-sitter)",
                self.neighbours_text)
        section("T3 recipes (learned patterns)", self.t3_text)
        section("Similar past tickets", self.similar_tickets_text)
        section("Repo doc (CLAUDE.md / README.md tail)",
                self.repo_doc_text)
        section("Operator memory (~/.claude/memory)",
                self.operator_memory_text)
        section("Unified memory hits", self.memory_hits_text)

        if self.commands:
            out.append(
                "## Build / test commands\n"
                + "\n".join(f"- `{k}`: `{v}`" for k, v in self.commands.items() if v)
            )
        if self.acceptance:
            out.append(
                "## Acceptance criteria\n"
                + "\n".join(f"- {a}" for a in self.acceptance[:10])
            )
        if self.sources_used:
            out.append("_sources: " + ", ".join(self.sources_used) + "_")
        return "\n\n".join(out)


# ───────── orchestrator ───────────────────────────────────────────


class UnifiedContext:
    """One stop shop. Stateless per call (cheap to instantiate)."""

    def for_intent(self, intent: "Intent", *, role: str = "sr_developer",
                   token_budget: int = 4000) -> ContextBundle:
        bundle = ContextBundle(intent=intent)
        # Repo guess + worktree path (used by repo_map, standards, doc)
        repo = _resolve_repo(intent)
        bundle.repo = repo
        worktree = _worktree_for(repo) if repo else ""

        # 1. Symbol vector + ticket + doc + external + related (the
        #    aggregator). Provides ranked text hits + ticket brief.
        try:
            from aiforge_core.memory.unified_query import (
                query as _uq, render as _ur,
            )
            uq = _uq(intent.search_query(), role=role,
                     ticket=None, limit=10)
            bundle.memory_hits_text = _ur(uq)[: token_budget // 4]
            bundle.sources_used.extend(uq.get("used_sources") or [])
            # Pull file paths out of symbol hits to seed focal_files.
            focal = _extract_focal_files(uq.get("hits") or [], worktree)
            bundle.focal_files = focal[:8]
        except Exception as exc:
            bundle.errors.append(f"unified_query: {exc}")

        # 2. RepoMap — keyed off focal_files (now non-empty)
        if worktree and bundle.focal_files:
            try:
                from aiforge_core.memory.code_context import aider_digest
                bundle.repo_map_text = aider_digest(
                    worktree, chat_files=bundle.focal_files,
                    token_budget=min(1024, token_budget // 4),
                )
                if bundle.repo_map_text:
                    bundle.sources_used.append("aider_repomap")
            except Exception as exc:
                bundle.errors.append(f"aider: {exc}")

        # 3. Graph neighbours — same focal seed
        if bundle.focal_files:
            try:
                from aiforge_core.memory.code_context import graph_neighbours
                bundle.neighbours_text = graph_neighbours(
                    bundle.focal_files, limit=30,
                )
                if bundle.neighbours_text:
                    bundle.sources_used.append("graph_neighbours")
            except Exception as exc:
                bundle.errors.append(f"graph: {exc}")

        # 4. Repo standards (commands, conventions, acceptance)
        if repo:
            try:
                from aiforge_core.runtime import repo_standards as _rs
                std = _rs.get(repo, worktree=worktree or None)
                bundle.standards_text = _rs.render(std)
                bundle.commands = {
                    "build": std.build_cmd, "compile": std.compile_cmd,
                    "test": std.test_cmd, "lint": std.lint_cmd,
                    "format": std.format_cmd,
                }
                bundle.acceptance = list(std.acceptance_criteria or [])
                bundle.sources_used.append("repo_standards")
            except Exception as exc:
                bundle.errors.append(f"standards: {exc}")

        # 5. Similar past tickets (Postgres text search on title+body).
        # Key off entity (single token) — full search_query is too long
        # for an ILIKE %...% match.
        try:
            sim_key = intent.entity or intent.reference_pattern or ""
            sims = _similar_tickets(sim_key, intent.keywords[:3], limit=5)
            if sims:
                bundle.similar_tickets = sims
                bundle.similar_tickets_text = "\n".join(
                    f"- **{s['identifier']}** ({s['status']}) — {s['title'][:90]}"
                    for s in sims
                )
                bundle.sources_used.append("similar_tickets")
        except Exception as exc:
            bundle.errors.append(f"similar_tickets: {exc}")

        # 6. T3 patterns / recipes
        try:
            recipes = _t3_recipes(intent.search_query(), limit=4)
            if recipes:
                bundle.t3_recipes = recipes
                bundle.t3_text = "\n".join(f"- {r}" for r in recipes)
                bundle.sources_used.append("t3_patterns")
        except Exception as exc:
            bundle.errors.append(f"t3: {exc}")

        # 7. CLAUDE.md / README.md tail
        if worktree:
            doc = _repo_doc(worktree)
            if doc:
                bundle.repo_doc_text = doc
                bundle.sources_used.append("repo_doc")

        # 8. Operator's ~/.claude/memory grep
        op = _operator_memory(intent.search_query(), limit=15)
        if op:
            bundle.operator_memory_text = op
            bundle.sources_used.append("claude_memory")

        # Reference files = focal_files filtered by the
        # reference_pattern token (e.g. "businessProducts")
        if intent.reference_pattern and bundle.focal_files:
            ref_token = intent.reference_pattern.lower()
            bundle.reference_files = [
                p for p in bundle.focal_files
                if ref_token in os.path.basename(p).lower()
            ][:5]

        return bundle

    # Convenience wrappers ──────────────────────────────────────────

    def for_chat(self, text: str, *, role: str = "sr_developer",
                 token_budget: int = 3000) -> ContextBundle:
        from aiforge_core.intent.classifier import classify
        return self.for_intent(classify(text), role=role,
                               token_budget=token_budget)

    def for_planner(self, ticket: object, *,
                    token_budget: int = 4000) -> ContextBundle:
        cached = _from_cached(ticket)
        if cached is not None:
            return cached
        return self._fresh_for_ticket(ticket, token_budget=token_budget)

    def for_doer(self, ticket: object, *,
                 token_budget: int = 4500) -> ContextBundle:
        cached = _from_cached(ticket)
        if cached is not None:
            return cached
        return self._fresh_for_ticket(ticket, token_budget=token_budget)

    def _fresh_for_ticket(self, ticket: object, *,
                          token_budget: int) -> ContextBundle:
        """Full fanout — used when ticket has no cached enrichment.
        IntentAgent stage 0 normally populates the cache; this is the
        backstop for tickets that bypass the workflow (legacy tests,
        manual ad-hoc runs, etc)."""
        from aiforge_core.intent.classifier import classify
        text = (
            f"{getattr(ticket, 'title', '')}\n"
            f"{getattr(ticket, 'body', '')}"
        )
        intent = classify(text)
        proj = getattr(ticket, "project", None)
        if proj and not intent.repo_hint:
            intent.repo_hint = proj
        return self.for_intent(intent, token_budget=token_budget)


# ───────── helpers ────────────────────────────────────────────────


def _from_cached(ticket: object) -> ContextBundle | None:
    """Build a ContextBundle from ``ticket.metadata.enrichment`` written
    by AiForgeIntentAgent. Returns ``None`` when no cached enrichment
    is present (caller falls back to full fanout)."""
    md = getattr(ticket, "metadata", None) or {}
    if not isinstance(md, dict):
        return None
    enr = md.get("enrichment")
    if not isinstance(enr, dict) or not enr.get("intent"):
        return None
    intent_d = enr.get("intent") or {}
    # Rehydrate Intent dataclass without re-importing classifier eagerly.
    from aiforge_core.intent.classifier import Intent
    intent = Intent(
        action=intent_d.get("action") or "investigate",  # type: ignore[arg-type]
        entity=intent_d.get("entity") or "",
        reference_pattern=intent_d.get("reference_pattern") or "",
        repo_hint=intent_d.get("repo_hint") or "",
        keywords=list(intent_d.get("keywords") or []),
        raw_text=getattr(ticket, "body", "") or "",
    )
    bundle = ContextBundle(
        intent=intent,
        repo=enr.get("repo") or "",
        focal_files=list(enr.get("focal_files") or []),
        reference_files=list(enr.get("reference_files") or []),
        similar_tickets=list(enr.get("similar_tickets") or []),
        t3_recipes=list(enr.get("t3_recipes") or []),
        commands=dict(enr.get("commands") or {}),
        acceptance=list(enr.get("acceptance") or []),
        sources_used=list(enr.get("sources_used") or []) + ["cache"],
        errors=list(enr.get("errors") or []),
    )
    if bundle.similar_tickets:
        bundle.similar_tickets_text = "\n".join(
            f"- **{s.get('identifier','?')}** ({s.get('status','?')}) — "
            f"{(s.get('title','') or '')[:90]}"
            for s in bundle.similar_tickets
        )
    if bundle.t3_recipes:
        bundle.t3_text = "\n".join(f"- {r}" for r in bundle.t3_recipes)
    return bundle


def _resolve_repo(intent: "Intent") -> str:
    """Best-guess repo name. Prefer explicit hint; else scan keywords
    against existing worktree dirs."""
    hint = (intent.repo_hint or "").strip()
    if hint:
        return hint
    try:
        candidates = {
            d for d in os.listdir(WORKTREE_ROOT)
            if os.path.isdir(os.path.join(WORKTREE_ROOT, d))
        }
    except FileNotFoundError:
        candidates = set()
    haystack = " ".join([intent.entity, intent.reference_pattern,
                         *intent.keywords]).lower()
    for c in candidates:
        if c.lower() in haystack:
            return c
    return ""


def _worktree_for(repo: str) -> str:
    p = os.path.join(WORKTREE_ROOT, repo)
    return p if os.path.isdir(p) else ""


_SRC_HINT_RE = re.compile(r"src/(?:main|test)/[\w/.-]+\.(?:java|py|ts|tsx|js|kt|go)")


def _extract_focal_files(hits: list[dict], worktree: str) -> list[str]:
    """Pull plausible file paths out of unified_query hits."""
    out: list[str] = []
    seen: set[str] = set()
    for h in hits:
        text = (h.get("text") or "") + " " + (h.get("source_uri") or "")
        for m in _SRC_HINT_RE.findall(text):
            if m in seen:
                continue
            seen.add(m)
            # Resolve under worktree if it exists; else keep relative.
            if worktree:
                full = os.path.join(worktree, m)
                if os.path.isfile(full):
                    out.append(full)
                    continue
            out.append(m)
    return out


def _similar_tickets(primary: str, extra_keys: list[str] | None = None,
                     *, limit: int = 5) -> list[dict]:
    """Postgres ILIKE on title+body. Per-keyword OR query so any single
    matching token surfaces the ticket. Past blocked/done both useful."""
    keys = [k for k in [primary, *(extra_keys or [])] if k and k.strip()]
    keys = list(dict.fromkeys(keys))[:5]   # de-dupe, cap
    if not keys:
        return []
    try:
        from aiforge_core.runtime import tickets as _t
    except Exception:
        return []
    clauses = []
    params: list = []
    for k in keys:
        pat = f"%{k.replace('%', '')[:60]}%"
        clauses.append("title ILIKE %s")
        clauses.append("body ILIKE %s")
        params.extend([pat, pat])
    where = " OR ".join(clauses)
    sql = (
        "SELECT identifier, title, status, "
        "       to_char(updated_at,'YYYY-MM-DD') AS updated "
        f"FROM tickets WHERE {where} "
        "ORDER BY updated_at DESC LIMIT %s"
    )
    params.append(limit)
    try:
        with _t._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _t3_recipes(query: str, *, limit: int = 4) -> list[str]:
    """Pull learner-written + auto-promoted T3 facts matching query."""
    if not query.strip():
        return []
    try:
        from aiforge_core.runtime.memory import Memory
        hits = Memory().search(query, role="sr_developer", top_k=limit * 3)
    except Exception:
        return []
    out: list[str] = []
    for h in hits:
        tier = (getattr(h, "tier", "") or "").lower()
        wing = (getattr(h, "wing", "") or "").lower()
        if tier != "t3":
            continue
        if not (wing.startswith("patterns/") or wing.startswith("skills/")):
            continue
        text = (getattr(h, "text", "") or "").strip().replace("\n", " ")
        if text:
            out.append(text[:280])
        if len(out) >= limit:
            break
    return out


def _repo_doc(worktree: str) -> str:
    """Return CLAUDE.md tail (preferred) else README.md tail. ~1.5K chars."""
    for fn in ("CLAUDE.md", "README.md", ".aiforge/CONVENTIONS.md"):
        path = os.path.join(worktree, fn)
        if os.path.isfile(path):
            try:
                txt = Path(path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            return f"[{fn}]\n{txt[:1500]}"
    return ""


def _operator_memory(query: str, *, limit: int = 15) -> str:
    """Grep-style hit list across ~/.claude/memory/*.md."""
    mem_dir = os.environ.get(
        "AIFORGE_CLAUDE_MEMORY_DIR",
        os.path.expanduser("~/.claude/memory"),
    )
    if not os.path.isdir(mem_dir) or not query.strip():
        return ""
    pat = re.compile(re.escape(query[:40]), re.IGNORECASE)
    hits: list[str] = []
    try:
        names = sorted(os.listdir(mem_dir), reverse=True)
    except Exception:
        return ""
    for fn in names:
        if not fn.endswith(".md"):
            continue
        path = os.path.join(mem_dir, fn)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if pat.search(line):
                        hits.append(f"{fn}:{i}: {line.rstrip()[:160]}")
                        if len(hits) >= limit:
                            return "\n".join(hits)
        except Exception:
            continue
    return "\n".join(hits)
