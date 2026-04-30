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
    codemem_text: str = ""            # codemem.query rendered bundle (new path)
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
                "## Edit targets (write here — pattern enumerated in this file)\n"
                + "\n".join(f"- {p}" for p in self.focal_files[:12])
            )
        if self.reference_files:
            out.append(
                "## Reference files (READ ONLY — do NOT edit; mention only)\n"
                + "\n".join(f"- {p}" for p in self.reference_files[:8])
            )
        # codemem block goes near the top — it carries the most
        # surgical anchors (services + summarized files + symbols).
        section("Codemem (4-level: Repo→Service→File_v2→Symbol_v2)",
                self.codemem_text)
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
                   token_budget: int = 4000,
                   exclude_identifier: str | None = None) -> ContextBundle:
        bundle = ContextBundle(intent=intent)
        # Repo guess + worktree path (used by repo_map, standards, doc)
        repo = _resolve_repo(intent)
        bundle.repo = repo
        worktree = _worktree_for(repo) if repo else ""

        # codemem — additive source, opt-in via AIFORGE_CODEMEM_QUERY=1.
        # Soft-fail; the rest of the aggregator continues regardless.
        if os.environ.get("AIFORGE_CODEMEM_QUERY") == "1" and repo:
            try:
                from aiforge_memory.api.read import context_bundle_for
                cm_text = context_bundle_for(
                    intent.raw_text or intent.search_query(),
                    repo=repo, role=role,
                    token_budget=max(1000, token_budget // 2),
                )
                if cm_text:
                    bundle.codemem_text = cm_text
                    bundle.sources_used.append("codemem")
            except Exception as exc:
                bundle.errors.append(f"codemem: {exc}")

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

        # 1b. Cursor-style semantic fallback. When regex-mined focal
        # files are empty (extraction failed OR symbol hits didn't
        # render paths in their rendered text), embed the raw natural-
        # language query and hit Neo4j's :Symbol vector index
        # directly. Each hit IS a file_path, no parsing needed.
        if worktree and not bundle.focal_files:
            try:
                semantic = _semantic_focal_files(
                    worktree, intent.raw_text or intent.search_query(),
                    top_k=8,
                )
                if semantic:
                    bundle.focal_files = semantic
                    bundle.sources_used.append("semantic_focal")
            except Exception as exc:
                bundle.errors.append(f"semantic_focal: {exc}")

        # 2. RepoMap — aider's PageRank personalised by mentioned_idents
        # / mentioned_fnames extracted from intent.raw_text. Aider's own
        # algorithm (get_ident_mentions + get_file_mentions). Pass the
        # raw user text so the digest centres on what they actually said.
        if worktree:
            try:
                from aiforge_core.memory.code_context import aider_digest
                bundle.repo_map_text = aider_digest(
                    worktree, chat_files=bundle.focal_files,
                    token_budget=min(1024, token_budget // 4),
                    user_text=intent.raw_text or intent.search_query(),
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

        # 5. Similar past tickets — semantic via bge-m3 cosine.
        # ILIKE prefilters to ≤60 candidates by entity/keywords;
        # embed + cosine ranks them. Threshold 0.3 drops noise so an
        # 'add Districts' ticket no longer ranks above an unrelated
        # past ticket just because both had the word 'add'.
        try:
            sim_key = intent.entity or intent.reference_pattern or ""
            sims = _similar_tickets(
                sim_key, intent.keywords[:3], limit=5,
                query_text=intent.raw_text or intent.search_query(),
                exclude_identifier=exclude_identifier,
            )
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

        # Reference-pattern split — when the ticket says "like
        # businessProducts", classify every file mentioning that token
        # into EDIT TARGETS (where the pattern is enumerated in a
        # collection literal / case / annotation — i.e. doer adds a
        # sibling entry) vs REFERENCE-ONLY (passing mentions in
        # imports, comments, calls — read-only context).
        # Match-count heuristic, no AST. Detail in _classify_ref_files.
        if worktree and intent.reference_pattern:
            edits, refs = _classify_ref_files(
                worktree, intent.reference_pattern, max_files=8,
            )
            if edits or refs:
                # Edit targets become the *new* focal_files — the
                # ScopeGuard write allowlist. Anything else stays in
                # reference_files (read-only). Existing focal_files
                # extracted from unified_query hits get pushed into
                # reference too (they came from semantic search, not
                # the literal-pattern enumeration check).
                old_focal = bundle.focal_files or []
                bundle.focal_files = edits[:4]
                ref_combined = list(dict.fromkeys(refs + old_focal))
                bundle.reference_files = [
                    p for p in ref_combined if p not in bundle.focal_files
                ][:6]
                bundle.sources_used.append("ref_pattern_split")
        # Fallback: when no reference_pattern OR splitter found
        # nothing, keep the old basename-based reference filter so
        # bundles still surface SOMETHING.
        if (intent.reference_pattern and bundle.focal_files
                and not bundle.reference_files):
            ref_token = intent.reference_pattern.lower()
            bundle.reference_files = [
                p for p in bundle.focal_files
                if ref_token in os.path.basename(p).lower()
            ][:5]

        # ── Cleaning pass ─────────────────────────────────────────
        # Dedupe across sources, drop noise paths (target/, .pyc,
        # build artefacts), normalise hit scores, dedupe near-
        # duplicate content. Bundle is THE prompt input — noise here
        # = wasted tokens for every downstream agent.
        bundle.focal_files = _dedupe_paths(bundle.focal_files)[:8]
        bundle.reference_files = _dedupe_paths(bundle.reference_files)[:5]

        # Dedupe similar_tickets by identifier (Postgres ILIKE +
        # cached enrichment can both surface the same row).
        seen_t: set[str] = set()
        deduped_sims: list[dict] = []
        for s in bundle.similar_tickets:
            ident = s.get("identifier") or ""
            if ident and ident in seen_t:
                continue
            if ident:
                seen_t.add(ident)
            deduped_sims.append(s)
        bundle.similar_tickets = deduped_sims[:5]

        # Sources_used dedup so render footer reads cleanly.
        bundle.sources_used = list(dict.fromkeys(bundle.sources_used))

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
        return self.for_intent(
            intent, token_budget=token_budget,
            exclude_identifier=getattr(ticket, "identifier", None),
        )


# ───────── helpers ────────────────────────────────────────────────


# ───────── data cleaning ──────────────────────────────────────────
# All sources funnel through these helpers before the bundle is
# rendered. Goal: dedupe across sources, drop noise paths, normalise
# scores so cross-source ranking is meaningful. Noise filter is the
# shared aiforge_core.index.noise module — same definition the
# indexers use, so what's invisible to the index is invisible at
# query time too (and vice-versa, defense in depth).
from aiforge_core.index.noise import is_noise_path as _is_noise_path


def _dedupe_paths(paths: list[str]) -> list[str]:
    """Stable order-preserving dedup. Treats absolute and repo-relative
    paths as the same entry (suffix match) to collapse the case where
    aider returns absolute paths and graph returns repo-relative."""
    seen_basenames: set[str] = set()
    out: list[str] = []
    for p in paths:
        if not p or _is_noise_path(p):
            continue
        # Use basename + parent dir as identity to avoid the
        # `/abs/.../foo.java` vs `src/.../foo.java` double-count.
        parts = p.replace("\\", "/").split("/")
        key = "/".join(parts[-2:]) if len(parts) >= 2 else p
        if key in seen_basenames:
            continue
        seen_basenames.add(key)
        out.append(p)
    return out


def _normalise_hits(hits: list[dict], *, source_weights: dict[str, float]) -> list[dict]:
    """Score-normalise across sources so cross-source top-K is fair.

    Per-source: rescale scores to [0,1] then multiply by configured
    weight. Drop content-duplicates (first 240 chars equal — KISS
    fingerprint that catches near-duplicates from related vs sym
    vs memory hits without an embedding pass)."""
    by_source: dict[str, list[dict]] = {}
    for h in hits:
        by_source.setdefault(h.get("source") or "?", []).append(h)
    out: list[dict] = []
    seen_fp: set[str] = set()
    for src, group in by_source.items():
        weight = source_weights.get(src, 1.0)
        scores = [float(g.get("score") or 0.0) for g in group]
        max_s = max(scores) if scores else 1.0
        max_s = max_s if max_s > 0 else 1.0
        for h, s in zip(group, scores):
            h2 = dict(h)
            h2["score"] = (s / max_s) * weight
            txt = (h2.get("text") or "")[:240].strip()
            fp = txt.lower()
            if fp and fp in seen_fp:
                continue
            if fp:
                seen_fp.add(fp)
            out.append(h2)
    out.sort(key=lambda x: -float(x.get("score") or 0))
    return out


_DEFAULT_SOURCE_WEIGHTS = {
    "memory": 1.0, "ticket": 1.2, "related": 0.8, "symbol": 0.9,
    "doc": 0.6, "external": 0.4,
    "aider_repomap": 1.0, "graph_neighbours": 0.9,
    "t3_patterns": 0.85, "similar_tickets": 0.7,
    "repo_doc": 0.5, "claude_memory": 0.4,
    "cache": 1.0,
}


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
    """Resolve worktree dir name from intent.

    Order:
      1. Explicit hint (already validated by ticket POST or planner).
      2. Scan intent.raw_text (the user's actual ticket body) for any
         worktree dir name. Match LONGEST first so 'mongoEventListner'
         beats 'mongo'. Body wins because the user typed it
         literally — keywords are derived and noisier.
      3. Fall back to entity / ref / keywords (old behaviour).
    """
    hint = (intent.repo_hint or "").strip()
    if hint:
        return hint
    try:
        candidates = sorted(
            (d for d in os.listdir(WORKTREE_ROOT)
             if os.path.isdir(os.path.join(WORKTREE_ROOT, d))
             and not d.startswith(".")),
            key=len, reverse=True,   # longest first
        )
    except FileNotFoundError:
        return ""
    body = (intent.raw_text or "").lower()
    for c in candidates:
        # Word-boundary-ish: surround by non-letter so we don't match
        # 'mongo' inside 'mongoEventListner' before the longer name
        # gets a chance.
        cl = c.lower()
        if cl in body:
            return c
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
    """Pull plausible file paths out of unified_query hits.

    REPO-SCOPED: only returns paths that resolve to a real file under
    the resolved ``worktree``. Cross-repo paths are dropped."""
    if not worktree:
        return []
    out: list[str] = []
    seen: set[str] = set()
    # Prefer structured fields (file_path, file, path, source_uri) over
    # regex-mining the rendered text — many hit shapes embed the path
    # in the JSON response but don't include 'src/...' verbatim in the
    # rendered .text field.
    structured_keys = ("file_path", "file", "path", "source_uri", "uri")
    for h in hits:
        # Structured first.
        for k in structured_keys:
            v = h.get(k)
            if not isinstance(v, str) or not v:
                continue
            for m in _SRC_HINT_RE.findall(v):
                if m in seen:
                    continue
                seen.add(m)
                full = os.path.join(worktree, m)
                if os.path.isfile(full):
                    out.append(full)
        # Fall back to text scan.
        text = (h.get("text") or "")
        for m in _SRC_HINT_RE.findall(text):
            if m in seen:
                continue
            seen.add(m)
            full = os.path.join(worktree, m)
            if os.path.isfile(full):
                out.append(full)
    return out


def _semantic_focal_files(worktree: str, query: str, *,
                          top_k: int = 8) -> list[str]:
    """Cursor-style fallback — when regex/grep returned no focal_files,
    embed the FULL natural-language query and ask Neo4j for the top-K
    :Symbol nodes by cosine similarity, then dedupe to file paths.

    Skips silently when:
      - bge-m3 sidecar unreachable
      - Neo4j unreachable / vector index absent
      - no symbol vectors backfilled yet for this repo
    Returns paths under the resolved ``worktree`` only.
    """
    if not worktree or not query.strip():
        return []
    try:
        from aiforge_core.legacy.embed import embed
        from neo4j import GraphDatabase                  # type: ignore
    except Exception:
        return []
    try:
        qvec = embed(query[:1500])
    except Exception:
        return []
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    # Method-only — class_embedding_vec exists but has 0 entries,
    # UNION ALL fails on the empty side.
    cy = (
        "CALL db.index.vector.queryNodes('method_embedding_vec', "
        "$k, $vec) YIELD node, score "
        "RETURN coalesce(node.file_path, node.file, '') AS path, "
        "       score "
        "ORDER BY score DESC"
    )
    paths: list[str] = []
    seen: set[str] = set()
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
        with drv.session() as s:
            for r in s.run(cy, k=top_k * 4, vec=qvec).data():
                p = r.get("path") or ""
                if not p or _is_noise_path(p):
                    continue
                # Reduce abs path → repo-relative (suffix match).
                if "src/main/" in p:
                    p = p[p.index("src/main/"):]
                elif "src/test/" in p:
                    p = p[p.index("src/test/"):]
                if p in seen:
                    continue
                seen.add(p)
                full = os.path.join(worktree, p)
                if os.path.isfile(full):
                    paths.append(full)
                if len(paths) >= top_k:
                    break
        drv.close()
    except Exception:
        return []
    return paths


# ─── Reference-pattern → edit-targets vs reference-only splitter ───
# Per-line classifier. No AST, no LLM — pattern set covers the literal-
# collection idioms used in Java/Python/TS/Go for "this is where
# entries are enumerated", which is what makes a file an EDIT TARGET
# (the doer adds a new entry next to the reference one) rather than
# a passing reference (import / comment / random method call).

_REGISTRATION_RE = re.compile(
    r"\b(?:Set|List|Map|Arrays|Collections|ImmutableSet|ImmutableList|"
    r"ImmutableMap)\.(?:of|asList|copyOf)\b"
    r"|case\s+\"[^\"]*\"\s*:"        # case "X":
    r"|\benum\s+\w+"                 # enum declarations
    r"|@\w+\s*\("                    # annotation with values (e.g. @Topic({"x"}))
    r"|=\s*\{"                       # array literal opener
    r"|\bRegister\(|\bregister\("    # generic register fns
    # Lone string-literal in a multi-line collection literal — the
    # common Java/Kotlin idiom is `Set.of(\n  "X",\n  "Y"\n)` so the
    # line containing the actual token is JUST the quoted string +
    # optional comma. Treat that as a registration line too.
    r"|^\s*\"[^\"]+\"\s*,?\s*$",
)
_WIRING_RE = re.compile(
    r"\.equals\(\s*\""
    r"|\bif\s*\(\s*\""
    r"|\bcontains\(\s*\""
    r"|\bswitch\s*\("
    r"|@(?:Get|Post|Put|Delete|Request)Mapping\s*\("
)
_NOISE_LINE_RE = re.compile(
    r"^\s*(?:import\b|//|/\*|\*\s|#\s|@param\b|@see\b|@throws\b)"
)


def _classify_ref_files(worktree: str, pattern: str, *,
                        max_files: int = 8) -> tuple[list[str], list[str]]:
    """Split files containing ``pattern`` into (edit_targets, reference).

    Per-file score = 5×REGISTRATION + 2×WIRING + 1×CALL + 0×NOISE
    (where each axis is the count of lines matching that classifier).

    edit_targets : score ≥ 5 — file enumerates / registers the pattern;
                   doer should add a new entry adjacent to the existing
                   one. Top 4 by score.
    reference    : score < 5 — file mentions pattern in passing
                   (import, comment, signature). Top (max_files - 4) by
                   score, kept for read-only context.

    Empty (worktree=None / pattern=empty / ripgrep absent) returns
    ``([], [])`` — caller falls back to whatever it had."""
    if not worktree or not pattern.strip() or len(pattern) < 3:
        return [], []
    # Strip sentence punctuation that the LLM/heuristic classifier
    # often leaves on the reference token (e.g. 'businessProducts.').
    # ripgrep -F is literal so any extra char kills the match.
    pattern = pattern.strip().rstrip(".,;:!?\"'`)]}>")
    if len(pattern) < 3:
        return [], []
    import subprocess
    try:
        # -n: line numbers · --no-heading: one path per line · -F: literal
        proc = subprocess.run(
            ["rg", "-n", "-F", "--no-heading",
             "--type-add=code:*.{java,py,ts,tsx,js,kt,go}",
             "--type=code", pattern, worktree],
            capture_output=True, text=True, timeout=25,
        )
        if proc.returncode not in (0, 1):
            return [], []
    except Exception:
        return [], []

    per_file: dict[str, dict[str, int]] = {}
    for raw in (proc.stdout or "").splitlines():
        # Format: path:lineno:content
        try:
            path, _ln, content = raw.split(":", 2)
        except ValueError:
            continue
        if _is_noise_path(path):
            continue
        bucket = per_file.setdefault(path, {
            "registration": 0, "wiring": 0, "call": 0, "noise": 0,
            "score": 0, "lines": 0,
        })
        bucket["lines"] += 1
        if _NOISE_LINE_RE.match(content):
            bucket["noise"] += 1
            continue
        if _REGISTRATION_RE.search(content):
            bucket["registration"] += 1
            continue
        if _WIRING_RE.search(content):
            bucket["wiring"] += 1
            continue
        bucket["call"] += 1

    for path, b in per_file.items():
        b["score"] = 5 * b["registration"] + 2 * b["wiring"] + 1 * b["call"]

    ranked = sorted(per_file.items(), key=lambda kv: -kv[1]["score"])

    EDIT_THRESHOLD = 5
    edit_targets: list[str] = []
    reference: list[str] = []
    for path, b in ranked:
        if b["score"] >= EDIT_THRESHOLD and len(edit_targets) < 4:
            edit_targets.append(path)
        elif len(reference) < max(0, max_files - len(edit_targets)):
            reference.append(path)
    return edit_targets, reference


def _similar_tickets(primary: str, extra_keys: list[str] | None = None,
                     *, limit: int = 5,
                     query_text: str | None = None,
                     exclude_identifier: str | None = None) -> list[dict]:
    """Semantic similarity over Postgres tickets.

    Pipeline:
      1. ILIKE prefilter on entity + keywords → narrow N → ≤ 60 cands.
         Keeps embed cost bounded; avoids cosine over thousands of rows.
      2. Embed query + each candidate's `title || body`.
         bge-m3 1024-d via the AIFORGE_EMBED_URL sidecar.
      3. Cosine similarity. Sort desc. Return top K with `score`.

    Replaces the old `ANY ILIKE OR` ranker which surfaced lexically-
    overlapping but semantically-unrelated tickets ('add Districts' vs
    'add storeRegions' shared 'add'/'collection' but were different
    work). KISS: 60-row in-process embedding loop, no pgvector needed.

    Soft-fail to no-op (empty list) when sidecar is down — avoids
    crashing UC for tickets that simply have no similarity context.
    """
    keys = [k for k in [primary, *(extra_keys or [])] if k and k.strip()]
    keys = list(dict.fromkeys(keys))[:5]
    if not keys:
        return []
    try:
        from aiforge_core.runtime import tickets as _t
    except Exception:
        return []
    # 1. ILIKE prefilter — wide net, narrowed to 60 most recent.
    clauses, params = [], []
    for k in keys:
        pat = f"%{k.replace('%', '')[:60]}%"
        clauses.append("title ILIKE %s")
        clauses.append("body ILIKE %s")
        params.extend([pat, pat])
    where = " OR ".join(clauses)
    sql = (
        "SELECT identifier, title, body, status, "
        "       to_char(updated_at,'YYYY-MM-DD') AS updated "
        f"FROM tickets WHERE {where} "
        "ORDER BY updated_at DESC LIMIT 60"
    )
    candidates: list[dict] = []
    try:
        with _t._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c.name for c in cur.description]
            candidates = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []
    if not candidates:
        return []
    # 2. Embed + cosine. KISS: drop tickets we can't embed (e.g. empty body).
    qtext = (query_text or " ".join(keys))[:1000]
    try:
        from aiforge_core.legacy.embed import embed_batch
        cand_texts = [
            ((c.get("title") or "") + "\n" + (c.get("body") or ""))[:1500]
            for c in candidates
        ]
        # Single batch call — query first, then candidates.
        all_vecs = embed_batch([qtext] + cand_texts)
        if not all_vecs or len(all_vecs) != len(cand_texts) + 1:
            raise RuntimeError("embed_batch returned wrong shape")
        qv = all_vecs[0]
        cvs = all_vecs[1:]
    except Exception:
        # Sidecar down or empty input — fall back to recency-only order
        # of the ILIKE prefilter so callers still get something.
        return [
            {"identifier": c.get("identifier"),
             "title": c.get("title"),
             "status": c.get("status"),
             "updated": c.get("updated"),
             "score": 0.0}
            for c in candidates[:limit]
        ]
    # Pure-Python cosine — small N (<= 60), no numpy dependency.
    def _cos(a, b) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return (dot / (na * nb)) if na and nb else 0.0
    scored = []
    for c, cv in zip(candidates, cvs):
        ident = c.get("identifier")
        if exclude_identifier and ident == exclude_identifier:
            continue                               # drop self-match
        scored.append({
            "identifier": ident,
            "title": c.get("title"),
            "status": c.get("status"),
            "updated": c.get("updated"),
            "score": _cos(qv, cv),
        })
    # 3. Drop near-zero matches (true noise) — threshold KISS = 0.3.
    scored = [s for s in scored if s["score"] >= 0.3]
    scored.sort(key=lambda s: -s["score"])
    return scored[:limit]


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
    """Return repo doc tail. Lookup order:
      1. .aiforge/REPO_NOTES.md  — auto-generated structured contract
         (controllers, services, kafka/nats topics, mongo collections,
         build commands). Most useful for the agent because it lists
         the actual code surface.
      2. CLAUDE.md               — operator-curated long-form notes.
      3. README.md               — generic project intro.
      4. .aiforge/CONVENTIONS.md — Aider-style coding conventions.
    First match wins, ~3K chars (REPO_NOTES tend to be longer than
    plain README tails)."""
    for fn, cap in (
        (".aiforge/REPO_NOTES.md", 3000),
        ("CLAUDE.md", 1500),
        ("README.md", 1500),
        (".aiforge/CONVENTIONS.md", 1500),
    ):
        path = os.path.join(worktree, fn)
        if os.path.isfile(path):
            try:
                txt = Path(path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            return f"[{fn}]\n{txt[:cap]}"
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
