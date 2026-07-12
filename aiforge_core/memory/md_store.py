"""Markdown-file memory: human-readable notes on the filesystem that are
ALSO ingested into the searchable memory backend and shown in the UI.

Why: the app's knowledge memory (SQLite/Neo4j) is opaque — you can't `cat`
it or diff it in git. This keeps a plain ``.md`` file per memory under a
directory (``AIFORGE_MEMORY_MD_DIR``, default ``~/.aiforge/memory``) as the
human-facing source of truth, and mirrors each into the memory backend
(via the embedded SQLite store or AFM/Neo4j) so search + stats + the
Memory tab pick it up. Drop a ``.md`` file in the dir by hand and call
:func:`ingest_dir` to pull it in.

Each file carries YAML-ish frontmatter (name/kind/tags/source/created)
followed by the markdown body.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
import threading
import time as _time
from pathlib import Path

_log = logging.getLogger("aiforge.md_store")
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# Serialize read-modify-write on the .md files — two concurrent chat turns
# (or a chat turn + the auto-memory upsert) writing the same source file
# would otherwise lose one update (both read the same raw, both append, last
# write wins). Covers concurrency within this process.
_WRITE_LOCK = threading.Lock()

# Serializes compact() against ITSELF so two concurrent compactions can't read
# the same stale consolidated state and clobber each other. Held for the whole
# compact (incl. the slow LLM summarise) — but it is a DIFFERENT lock from
# _WRITE_LOCK, so a running compaction does NOT block ordinary chat-turn memory
# writes (upsert_section / append_bullet / write); only Phase 2's brief
# archive+write takes _WRITE_LOCK.
_COMPACT_LOCK = threading.Lock()


def memory_dir() -> Path:
    raw = os.environ.get("AIFORGE_MEMORY_MD_DIR") or os.path.join(
        os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge"), "memory")
    p = Path(os.path.expanduser(raw))
    p.mkdir(parents=True, exist_ok=True)
    return p


def briefs_dir() -> Path:
    """Subfolder holding the consolidated OKR briefs (``compacted-<scope>.md``),
    kept OUT of the memory-dir root (like ``memory-archive/``) so the root only
    holds transient per-run captures. Created lazily. ``migrate_briefs_to_folder``
    moves any legacy root-level briefs in here on startup."""
    p = memory_dir() / "compacted"
    p.mkdir(parents=True, exist_ok=True)
    return p


def brief_path(slug: str) -> Path:
    """Path to a brief file (``<memory>/compacted/compacted-<slug>.md``)."""
    return briefs_dir() / f"compacted-{slug}.md"


def iter_briefs() -> list[Path]:
    """Every consolidated brief file, sorted. Reads the ``compacted/`` subfolder
    AND (transitionally) any legacy root-level ``compacted-*.md`` not yet moved."""
    seen: dict[str, Path] = {}
    for p in briefs_dir().glob("compacted-*.md"):
        seen[p.name] = p
    for p in memory_dir().glob("compacted-*.md"):   # legacy root (pre-migration)
        seen.setdefault(p.name, p)
    return [seen[k] for k in sorted(seen)]


def _md_path_for_stem(stem: str) -> Path:
    """Path for a note stem: a brief (``compacted-*``) lives in the ``compacted/``
    subfolder, every other (per-run capture / session note) in the root."""
    if stem.startswith("compacted-"):
        return briefs_dir() / f"{stem}.md"
    return memory_dir() / f"{stem}.md"


def _brief_part_paths(base: str) -> list[Path]:
    """Split-part briefs ``compacted-<base>-N.md`` from the briefs folder AND the
    legacy root (pre-migration), de-duplicated by name, sorted."""
    seen: dict[str, Path] = {}
    for p in list(briefs_dir().glob(f"compacted-{base}-*.md")) \
            + list(memory_dir().glob(f"compacted-{base}-*.md")):
        seen.setdefault(p.name, p)
    return [seen[k] for k in sorted(seen)]


def _all_md_files() -> list[Path]:
    """Every md memory file — root-level per-run captures PLUS the briefs in the
    ``compacted/`` subfolder. De-duplicated by resolved path (a legacy brief may
    still sit in the root before migration)."""
    seen: dict[str, Path] = {}
    for p in list(memory_dir().glob("*.md")) + iter_briefs():
        try:
            seen[str(p.resolve())] = p
        except OSError:
            seen[str(p)] = p
    return list(seen.values())


def migrate_briefs_to_folder() -> dict:
    """Move legacy root-level ``compacted-*.md`` briefs into ``compacted/``.
    Idempotent; never raises. Skips captures (``<slug>-YYYYMMDD-<6hex>.md``)."""
    moved = 0
    bdir = briefs_dir()
    # ROOT-level briefs ONLY — a brief already in bdir must NEVER be touched
    # (iterating iter_briefs() here would compute dest==source and unlink the
    # live brief — a data-loss bug).
    for p in list(memory_dir().glob("compacted-*.md")):
        if p.parent == bdir:
            continue                                # already in the folder — skip
        if _CAPTURE_SIG_RE.search(p.name):
            continue                                # transient capture, not a brief
        dest = bdir / p.name
        try:
            if dest.exists():
                p.unlink()                          # already migrated → drop dup
            else:
                p.rename(dest)
            moved += 1
        except OSError:
            continue
    return {"ok": True, "moved": moved}


def _slug(title: str, maxlen: int = 80) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "note").lower()).strip("-")
    return (s or "note")[:maxlen]


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _parse(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta: dict = {}
    body = raw
    m = _FM_RE.match(raw)
    if m:
        body = m.group(2).strip()
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                # OKR-envelope briefs (work_notes) write JSON-quoted scalars
                meta[k.strip()] = v.strip().strip('"')
    return {
        "name": path.stem,
        "file": path.name,
        "title": meta.get("title") or meta.get("name") or path.stem,
        "kind": meta.get("kind") or "note",
        "tags": [t.strip() for t in (meta.get("tags") or "").split(",") if t.strip()],
        "source": meta.get("source") or "manual",
        "created": meta.get("created") or "",
        # repo + topic drive the two compaction axes (project brief / topic note).
        # topic falls back to a `topic:<slug>` tag when not an explicit field.
        "repo": meta.get("repo") or "",
        "topic": meta.get("topic") or "",
        "preview": body[:240],
        "body": body,
        "path": str(path),
    }


def list_files() -> list[dict]:
    """All md memories (newest first), without full body."""
    out = []
    for p in sorted(_all_md_files(), key=lambda x: x.name, reverse=True):
        try:
            d = _parse(p)
            d.pop("body", None)
            d["mtime"] = p.stat().st_mtime
            out.append(d)
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda d: d.get("created") or "", reverse=True)
    return out


def _resolve_md(name: str) -> Path | None:
    """Resolve a memory-file name to its path in the root OR the ``compacted/``
    briefs subfolder. Rejects any name with a path separator (traversal guard)."""
    fn = name if name.endswith(".md") else f"{name}.md"
    if os.path.basename(fn) != fn:
        return None
    for d in (memory_dir(), briefs_dir()):
        p = d / fn
        if p.is_file():
            return p
    return None


def read_file(name: str) -> dict | None:
    p = _resolve_md(name)
    return _parse(p) if p else None


def _brief_title(key: str) -> str:
    """Human title for a brief from its scope key: ``deployment`` → "Deployment",
    ``cicd-pipeline`` → "Cicd Pipeline". So search/UI show a real title, not the
    internal ``compacted-<key>`` stem."""
    return (key or "").replace("-", " ").replace("_", " ").strip().title() or key


def _ingest_unit(*, title: str, body: str, kind: str, tags: list[str],
                 source: str, repo: str, replace: bool = False) -> None:
    """Mirror a note into the active memory backend so it's searchable.
    ``replace=True`` deletes any prior row(s) with this ``source`` first — used
    for briefs so a re-ingest reclaims the old generation instead of piling up
    stale/orphan rows (incl. pre-scope-fix ``repo='notes'`` copies)."""
    text = f"{title}\n\n{body}".strip()
    from aiforge_core.memory import backend_select as _bsel
    try:
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
            if replace:
                # embed-only-on-change: if the source's row already holds this
                # exact text, leave it — skip a needless delete + re-embed.
                if _sqlmem.source_text_unchanged(source, f"{title}\n\n{body}".strip()):
                    return
                _sqlmem.delete_by_source(source)
            _sqlmem.write_unit(text=text, kind=kind, source=source,
                               tags=tags, metadata={"md": True}, repo=repo)
        else:
            from aiforge_core.runtime.tools.memory_write import memory_write
            # Forward the md:* source so memory_write's brief-feed skips it —
            # capture already maintains the (topic-aware) brief for this write.
            memory_write(text=text, kind=kind, tags=tags, repo=repo,
                         source=source)
    except Exception:  # noqa: BLE001
        pass  # md file is the source of truth; DB mirror is best-effort


def write(title: str, text: str, *, kind: str = "note",
          tags: list[str] | None = None, source: str = "manual",
          repo: str = "notes", topic: str | None = None,
          ingest: bool = True) -> dict:
    """Create an md memory file + ingest it into the searchable backend.

    ``repo`` and ``topic`` are written into the frontmatter (NOT just the DB
    mirror) so the compactor can group by them — the project-brief (per-repo)
    and topic-note (per-topic) axes read the md files, so anything that isn't
    stamped here simply won't roll up into either brief.

    W5: direct callers (e.g. the manual /api note endpoint) that leave the
    default ``repo="notes"`` create an UNSCOPED note — it won't roll up into any
    project/topic brief and is invisible to repo-scoped recall (only the global
    Memory search reaches it). That's intended for hand-dropped notes; scoped
    knowledge should go through ``capture()`` (which classifies + stamps)."""
    tags = list(tags or [])
    created = _now_iso()
    digest = hashlib.sha1((title + text).encode()).hexdigest()[:6]
    stem = f"{_slug(title)}-{created[:10].replace('-', '')}-{digest}"
    # RESERVED PREFIX GUARD: never let a per-note capture start with
    # ``compacted-``. compact() EXCLUDES every ``compacted-*`` file from its
    # live set (treats it as an already-canonical brief), so a masquerading
    # capture would slip past compaction FOREVER and pile up. Strip the prefix
    # (the date+hex suffix still keeps the name unique).
    stem = re.sub(r"^compacted[-_]+", "", stem) or f"note-{digest}"
    # CONTENT DEDUP: an identical note (same title+text → same digest) captured
    # on a different day would otherwise mint a NEW dated file. If one already
    # exists, reuse it — no duplicate md file, no re-ingest.
    for _ex in memory_dir().glob(f"*-{digest}.md"):
        try:
            _exd = _parse(_ex)
            if (_exd.get("title") or "") == title and (_exd.get("body") or "").strip() == (text or "").strip():
                _exd.pop("body", None)
                return _exd
        except Exception:  # noqa: BLE001
            continue
    path = _md_path_for_stem(stem)
    fm = (
        "---\n"
        f"title: {title}\n"
        f"kind: {kind}\n"
        f"tags: {', '.join(tags)}\n"
        f"source: {source}\n"
        f"repo: {repo or ''}\n"
        f"topic: {topic or ''}\n"
        f"created: {created}\n"
        "---\n\n"
    )
    path.write_text(fm + (text or "").strip() + "\n", encoding="utf-8")
    # ingest=False: md file only (compaction source) — used when the caller
    # already wrote this fact to the backend (e.g. the learner), so we don't
    # double-write the searchable store.
    if ingest:
        _ingest_unit(title=title, body=text, kind=kind, tags=tags,
                     source=f"md:{stem}", repo=repo)
    d = _parse(path)
    d.pop("body", None)
    return d


# ── Unified capture: the ONE entry every learning/comment flows through ───────
# Each category writes an md file (repo + topic stamped) so it lands in BOTH
# compaction axes. Without this write there is no md → nothing to compact →
# it never reaches project/topic memory. Categories map to `kind`:
#   user_comment      — something the user said to keep (verbatim intent)
#   learning          — a general lesson (cross-repo)
#   project_learning  — a lesson scoped to THIS repo (drives the project brief)
#   topic_learning    — a lesson about a theme/workflow (drives the topic note)
#   topic_suggestion  — a topic the USER asked us to track/organise around
_CAPTURE_KINDS = {
    "user_comment", "learning", "project_learning",
    "topic_learning", "topic_suggestion",
}


# ── scope classifier: global | project:<repo> | topic:<slug> ─────────────────
# A captured fact belongs to exactly one scope, which decides its brief axis:
#   global          → cross-project knowledge → compacted-shared.md
#   project:<repo>  → one repo only           → compacted-<repo>.md
#   topic:<slug>    → cross-cutting theme      → compacted-<slug>.md
# The caller's repo/topic args are HINTS; an LLM (learner role, config-driven)
# may PROMOTE a repo-hinted but universally-true fact to global — the "is this
# global or per-project?" decision the user asked compaction to understand.
_SCOPE_SYS = (
    "Classify ONE captured knowledge item into its memory SCOPE. BE "
    "CONSERVATIVE: when in doubt choose PROJECT, not global. Global is a high bar.\n"
    "- global  : TRUE FOR EVERY REPOSITORY regardless of which one — a coding or "
    "git convention, a language/tool-level lesson (Java, Python, Maven, Gradle), "
    "a machine/environment gotcha. It must NOT mention any specific repo, "
    "library, service, file, or function.\n"
    "- project : about ONE repository/service — its code, endpoints, bugs, "
    "config, or a named file/function INSIDE it (e.g. src/requests/utils.py, a "
    "Session method, a service's endpoints). Transient build/test STATUS and a "
    "task STEP are project, never global.\n"
    "- topic   : a cross-cutting theme/workflow spanning repos (e.g. data-sync, "
    "auth, ci) that is neither a single repo nor universal.\n"
    "PREFER the caller's hint. Only OVERRIDE a repo hint to global when the item "
    "is unmistakably universal (would you tell it to a developer on a completely "
    "unrelated project? if not, it is NOT global). Reply scope plus, for project "
    "the repo slug, for topic a short kebab-case topic slug."
)


def classify_scope(text: str, *, hint_repo: str | None = None,
                   hint_topic: str | None = None, role: str = "learner") -> dict:
    """Decide a captured item's memory scope → ``{scope, repo, topic}``.

    Deterministic fallback (``AIFORGE_OKR_SCOPE_LLM=0`` or the model is
    unreachable): honour the hints — repo→project, else topic→topic, else
    global — so existing capture behaviour is unchanged. With the LLM on it may
    re-route a repo-hinted fact to global when it is universally true. Never
    raises."""
    hint_repo = (hint_repo or "").strip() or None
    hint_topic = (hint_topic or "").strip() or None

    def _fallback() -> dict:
        if hint_repo:
            return {"scope": "project", "repo": hint_repo, "topic": None}
        if hint_topic:
            return {"scope": "topic", "repo": None, "topic": _slug(hint_topic)}
        return {"scope": "global", "repo": None, "topic": None}

    body = (text or "").strip()
    if not body or os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return _fallback()
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class ScopeDecision(BaseModel):
            scope: str = ""
            repo: str = ""
            topic: str = ""

        hint = f"hint_repo={hint_repo or '-'} hint_topic={hint_topic or '-'}"
        res = structured_complete(
            role,
            [{"role": "system", "content": _SCOPE_SYS},
             {"role": "user", "content": f"{hint}\n\nITEM:\n{body[:2000]}"}],
            ScopeDecision, max_tokens=200, max_retries=1, temperature=0.0)
        scope = (getattr(res, "scope", "") or "").strip().lower()
        repo = (getattr(res, "repo", "") or "").strip() or None
        topic = (getattr(res, "topic", "") or "").strip() or None
    except Exception:  # noqa: BLE001 — model down / bad JSON → honour hints
        return _fallback()

    if scope == "global":
        return {"scope": "global", "repo": None, "topic": None}
    if scope == "project":
        # An explicit PROJECT verdict is NOT global even when the model names no
        # repo (repo=None is a valid "belongs to some project" answer). Fall back
        # to hints only for the repo label, never to a different SCOPE.
        r = _slug(repo) if repo else hint_repo
        return {"scope": "project", "repo": r, "topic": None}
    if scope == "topic":
        t = topic or hint_topic
        return {"scope": "topic", "repo": None,
                "topic": _snap_topic(_slug(t)) if t else None}
    return _fallback()   # only an empty/unknown scope falls back to the hints


def _snap_topic(slug: str) -> str:
    """Snap a freshly-minted topic slug to an EXISTING topic brief when they're
    near-identical (fuzzy) — stops the classifier proliferating
    ``sync-retries`` / ``sync-retry-policy`` / ``sync-retry`` into three briefs.
    Falls back to the slug when nothing close exists. Never raises."""
    if not slug:
        return slug
    try:
        import difflib
        existing = [p.stem[len("compacted-"):]
                    for p in iter_briefs()
                    if p.stem != "compacted-shared"
                    and not _CAPTURE_SIG_RE.search(p.name)]
        if slug in existing:
            return slug
        m = difflib.get_close_matches(slug, existing, n=1, cutoff=0.82)
        return m[0] if m else slug
    except Exception:  # noqa: BLE001
        return slug


# ── cross-scope mapping: link related briefs (project ↔ global ↔ topic) ───────
_CAPTURE_SIG_RE = re.compile(r"-\d{8}-[0-9a-f]{6}\.md$")  # per-run capture stamp


def _live_briefs() -> list[dict]:
    """Canonical scope briefs (``compacted-<scope>.md``), excluding per-run
    capture masqueraders. Each: ``{key, file, path, summary}``."""
    from aiforge_core.runtime import work_notes
    out: list[dict] = []
    for p in iter_briefs():
        if _CAPTURE_SIG_RE.search(p.name):
            continue
        key = p.stem[len("compacted-"):]
        if not key:
            continue
        try:
            d = _parse(p)
            summary = work_notes.knowledge_text(d.get("body") or "")[:200]
        except Exception:  # noqa: BLE001
            continue
        out.append({"key": key, "file": p.name, "path": p, "summary": summary})
    return out


_MAP_SYS = (
    "You relate KNOWLEDGE-MEMORY briefs across scopes. Each brief is one scope: "
    "a project (a repo), a cross-cutting topic, or 'shared' (global knowledge).\n"
    "Link two briefs ONLY when they document the SAME SPECIFIC subject at "
    "different scopes — e.g. a repo's branch rule and the global branch-naming "
    "convention, or a service's sync code and the cross-cutting data-sync topic. "
    "The link must be load-bearing: reading one brief, you would want the other.\n"
    "Be STRICT. Do NOT link two briefs merely because they fall in the same broad "
    "area (both about 'build', both about 'cache', both about 'tests'). MOST "
    "briefs have NO link — returning few or zero edges is correct and expected. "
    "When unsure, DON'T link.\n"
    "Use the EXACT keys given. Return JSON: a list \"edges\", each item "
    '{"a": "<exact key>", "b": "<exact key>"}.'
)


def _order_briefs_by_similarity(briefs: list[dict]) -> list[dict]:
    """Reorder briefs so EMBEDDING-similar ones are adjacent (greedy
    nearest-neighbour chain), so map_scopes' fixed-size batches co-present
    topically-related briefs regardless of NAME — alphabetical batching could
    never link ``auth-service`` ↔ ``login-flow``. Falls back to the input order
    if embeddings are unavailable. Never raises."""
    try:
        from aiforge_core.memory import local_embed
        vecs = {}
        for b in briefs:
            v = local_embed.embed((b.get("summary") or b.get("key") or "")[:400])
            if any(v):
                vecs[b["key"]] = v
        if len(vecs) < 3:
            return briefs

        def _cos(a, c):
            num = sum(x * y for x, y in zip(a, c))
            da = sum(x * x for x in a) ** 0.5
            dc = sum(y * y for y in c) ** 0.5
            return num / (da * dc) if da and dc else 0.0

        remaining = [b for b in briefs if b["key"] in vecs]
        tail = [b for b in briefs if b["key"] not in vecs]
        ordered = [remaining.pop(0)]
        while remaining:
            last = vecs[ordered[-1]["key"]]
            best_i, best_s = 0, -2.0
            for i, b in enumerate(remaining):
                s = _cos(last, vecs[b["key"]])
                if s > best_s:
                    best_s, best_i = s, i
            ordered.append(remaining.pop(best_i))
        return ordered + tail
    except Exception:  # noqa: BLE001 — no embedder → keep the given order
        return briefs


def map_scopes(*, role: str = "learner", dry_run: bool = False) -> dict:
    """Link related scope briefs BIDIRECTIONALLY: an LLM proposes which briefs
    share subject matter (a project ↔ the global/topic brief it relates to) and
    each gets a same-dir mapping link to the other in its Links section. Gated on
    ``AIFORGE_OKR_SCOPE_LLM`` (off → no-op). Never raises."""
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return {"edges": 0, "skipped": "llm_off"}
    briefs = _live_briefs()
    if len(briefs) < 2:
        return {"edges": 0}
    by_key = {b["key"]: b for b in briefs}
    # Order by EMBEDDING similarity (not alphabetical) so a fixed-size batch
    # co-presents topically-related briefs even when their names differ — the
    # alphabetical batching left ~88% of cross-name pairs never co-presented.
    briefs = _order_briefs_by_similarity(briefs)
    lines = [f"- {b['key']}: {b['summary']}" for b in briefs]
    # BATCH so each call's listing fits the input budget — a flat listing[:cap]
    # silently hides most briefs once there are 100s of them (the edges=0 bug).
    # Small batches keep each call fast (~10s for ~35 briefs on a local 122B);
    # a big single listing times out on a cold model. AIFORGE_OKR_MAP_INPUT_CHARS
    # tunes it.
    try:
        cap = max(1500, int(os.environ.get("AIFORGE_OKR_MAP_INPUT_CHARS", "6000")))
    except (TypeError, ValueError):
        cap = 6000
    batches: list[list[str]] = []
    buf: list[str] = []
    used = 0
    for ln in lines:
        if used and used + len(ln) > cap:
            batches.append(buf)
            buf, used = [], 0
        buf.append(ln)
        used += len(ln) + 1
    if buf:
        batches.append(buf)
    raw_edges: list = []
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete
    except Exception as exc:  # noqa: BLE001
        _log.debug("map_scopes: import failed: %s", exc)
        return {"edges": 0, "error": "llm_unreachable"}

    class _Edges(BaseModel):
        edges: list[dict] = []

    # Per-batch fault isolation: one slow/failed batch (e.g. a cold-load timeout)
    # must NOT discard the edges the other batches already produced.
    failed = 0
    for i, batch in enumerate(batches, 1):
        try:
            res = structured_complete(
                role,
                [{"role": "system", "content": _MAP_SYS},
                 {"role": "user", "content": "\n".join(batch)}],
                _Edges, max_tokens=1200, max_retries=1, temperature=0.0)
            raw_edges.extend(getattr(res, "edges", None) or [])
        except Exception as exc:  # noqa: BLE001 — skip this batch, keep the rest
            failed += 1
            _log.warning("map_scopes: batch %d/%d failed: %s", i, len(batches), exc)
    if failed and not raw_edges:
        return {"edges": 0, "error": "llm_unreachable"}

    def _edge_key(e: dict, *names: str) -> str:
        for nm in names:
            v = e.get(nm)
            if v:
                return str(v).strip()
        return ""

    try:
        max_links = max(1, int(os.environ.get("AIFORGE_OKR_MAP_MAX_LINKS", "3")))
    except (TypeError, ValueError):
        max_links = 3
    adj: dict[str, set[str]] = {}
    n = 0
    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        # models return {a,b} OR {from,to} OR {source,target} — accept all
        a = _edge_key(e, "a", "from", "source")
        b = _edge_key(e, "b", "to", "target")
        if a not in by_key or b not in by_key or a == b:
            continue
        if b in adj.get(a, set()):
            continue                       # already counted this undirected pair
        # Cap fan-out per brief so a loosely-linking model can't over-connect one
        # brief to a dozen others — skip the edge once EITHER end is full.
        if len(adj.get(a, ())) >= max_links or len(adj.get(b, ())) >= max_links:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
        n += 1
    if dry_run:
        return {"edges": n, "adj": {k: sorted(v) for k, v in adj.items()}}

    # Mapping is DERIVED and fully recomputed each run: strip every brief's
    # existing sibling-brief links (keep real URLs / jira refs) and rewrite from
    # the fresh adjacency, so a re-run with a tighter prompt REMOVES stale/loose
    # links instead of piling more on. Touch ALL briefs (not just adj) so a brief
    # that lost all its links this pass is cleaned too.
    from aiforge_core.runtime import work_notes
    updated: list[str] = []
    with _WRITE_LOCK:
        for b in briefs:
            key = b["key"]
            try:
                parsed = work_notes.parse_note(
                    b["path"].read_text(encoding="utf-8"))
            except OSError:
                continue
            existing = list(parsed["sections"].get("links") or [])
            kept = [l for l in existing if not work_notes._BRIEF_REF_RE.match(l)]
            fresh = kept + [f"[{t}]({by_key[t]['file']})"
                            for t in sorted(adj.get(key, ()))]
            if fresh == existing:
                continue                        # nothing changed for this brief
            work_notes.update_note(str(b["path"]), links=fresh,
                                   kind="knowledge", key=key)
            if adj.get(key):
                updated.append(key)
    return {"edges": n, "updated": sorted(updated)}


def _brief_file_of_source(source: str) -> str:
    """Resolve a search-hit ``source`` id back to its brief FILE name.

    Brief rows are ingested with source ``compacted:<stem>`` (Phase-3 /
    ingest_dir) or the legacy ``md:<stem>``; both map to ``<stem>.md`` where
    ``<stem>`` is ``compacted-<scope>``. Returns "" for a non-brief source."""
    s = str(source or "").strip()
    for pfx in ("compacted:", "md:"):
        if s.startswith(pfx):
            stem = s[len(pfx):]
            if stem.startswith("compacted-"):
                return stem + ".md"
    return ""


def expand_links(sources, *, max_links: int = 6, depth: int = 1) -> list[dict]:
    """Follow the **Links** section of each hit brief to its sibling briefs and
    return their FULL knowledge text.

    Search returns the briefs that matched the query; ``map_scopes`` has already
    wired each brief to its load-bearing neighbours (``[title](compacted-x.md)``
    refs in the Links section). This walks those edges so a hit surfaces the
    connected briefs' full content too — "search goes through the links and
    gives full info". Breadth-first up to ``depth`` hops, capped at
    ``max_links`` unique briefs, EXCLUDING the origin briefs themselves. Never
    raises; returns ``[{key, file, source, text, kind}]``.
    """
    from aiforge_core.runtime import work_notes
    mdir = memory_dir()
    origin = {f for f in (_brief_file_of_source(s) for s in (sources or [])) if f}
    seen: set[str] = set(origin)
    out: list[dict] = []
    frontier = list(origin)
    hop = 0
    while frontier and hop < max(1, depth) and len(out) < max_links:
        nxt: list[str] = []
        for fname in frontier:
            p = mdir / fname
            if not p.exists():
                continue
            try:
                parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            for link in (parsed["sections"].get("links") or []):
                m = work_notes._BRIEF_REF_RE.match(link.strip())
                if not m:
                    continue                       # keep real URLs / jira refs out
                tgt = m.group(1)                   # compacted-<scope>.md
                if tgt in seen:
                    continue
                seen.add(tgt)
                tp = mdir / tgt
                if not tp.exists():
                    continue
                try:
                    d = _parse(tp)
                    text = work_notes.knowledge_text(d["body"]) or d["body"]
                except OSError:
                    continue
                key = tgt[len("compacted-"):-len(".md")]
                out.append({
                    "key": key, "file": tgt,
                    "source": f"linked:{tgt[:-len('.md')]}",
                    "kind": d.get("kind") or "knowledge",
                    "title": _brief_title(key),
                    "text": text,
                })
                nxt.append(tgt)
                if len(out) >= max_links:
                    break
            if len(out) >= max_links:
                break
        frontier = nxt
        hop += 1
    return out[:max_links]


def _remove_facts_locked(path, key: str, remove_ci_keys: set) -> int:
    """Remove facts whose ``_ci_key(_fact_body(f))`` is in ``remove_ci_keys`` from
    a brief, re-reading it FRESH under ``_WRITE_LOCK`` so a concurrent capture
    (which also holds ``_WRITE_LOCK``) is never clobbered by a stale snapshot.
    Returns the count removed. Never raises on a bad path."""
    from aiforge_core.runtime import work_notes
    with _WRITE_LOCK:
        try:
            parsed = work_notes.parse_note(path.read_text(encoding="utf-8"))
        except OSError:
            return 0
        facts = parsed["sections"].get("facts") or []
        kept = [f for f in facts
                if work_notes._ci_key(_fact_body(f)) not in remove_ci_keys]
        removed = len(facts) - len(kept)
        if removed:
            work_notes.update_note(str(path), facts=kept, kind="knowledge",
                                   key=key)
        return removed


def reheal_scopes(*, role: str = "learner", max_per_brief: int = 60) -> dict:
    """Self-heal mis-scoped facts: re-classify each fact in every PROJECT/TOPIC
    brief and MOVE the ones that are actually global into the shared brief
    (facts captured before scope classification, or mis-hinted, end up in the
    wrong brief). The shared/global brief is never demoted into a project. Gated
    on ``AIFORGE_OKR_SCOPE_LLM`` (off → no-op). Never raises."""
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return {"moved": 0, "skipped": "llm_off"}
    from aiforge_core.runtime import work_notes
    moved = 0
    healed: list[str] = []
    # _COMPACT_LOCK (NOT _WRITE_LOCK): capture()→_brief_upsert takes _WRITE_LOCK,
    # so holding it here would self-deadlock. This serialises reheal against
    # compaction, which is the right granularity.
    with _COMPACT_LOCK:
        for b in _live_briefs():
            key = b["key"]
            if key == "shared":            # global brief — nothing to promote out
                continue
            try:
                parsed = work_notes.parse_note(b["path"].read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            if not facts:
                continue
            moved_facts: list[str] = []
            for f in facts[:max_per_brief]:
                try:
                    sc = classify_scope(f, hint_repo=key, role=role)
                except Exception:  # noqa: BLE001
                    continue
                if sc["scope"] != "global":
                    continue
                try:                              # → shared brief via capture
                    capture("learning", f, repo=None, classify=False,
                            source="reheal")
                except Exception:  # noqa: BLE001
                    continue                      # couldn't move → leave in place
                moved += 1
                moved_facts.append(f)
                # W4: drop the stale project-scoped INDEX row so the moved fact
                # isn't duplicated under both the old repo and 'shared'.
                try:
                    from aiforge_core.memory import backend_select, sqlite_memory
                    if backend_select.embedded():
                        # exclude 'compacted' so we delete the stale per-capture
                        # row, NOT the brief row (whose text contains every fact).
                        sqlite_memory.delete_by_text_contains(
                            _fact_body(f), repo=key, exclude_kind="knowledge")
                except Exception:  # noqa: BLE001
                    pass
            if not moved_facts:
                continue
            # Remove ONLY the moved facts, re-reading the brief FRESH under
            # _WRITE_LOCK — a concurrent capture that landed during the (slow)
            # classification is preserved instead of clobbered by a stale snapshot.
            _remove_facts_locked(b["path"], key, {
                work_notes._ci_key(_fact_body(x)) for x in moved_facts})
            healed.append(key)
    return {"moved": moved, "healed": healed}


_RECONCILE_SYS = (
    "You clean a set of knowledge-memory facts drawn from several scope briefs "
    "(each line: 'SCOPE :: fact'). Find DUPLICATES (same information, paraphrased "
    "across briefs) and CONTRADICTIONS (one fact supersedes another — a changed "
    "value / status / decision). For every REDUNDANT or STALE fact, output an "
    "item {scope, fact} to REMOVE, keeping the single best/newest version in ONE "
    "scope (prefer the broadest: shared > a topic > a project). Copy the fact "
    "text VERBATIM as given. Only remove genuine redundancy/contradiction — when "
    "unsure, keep it. Most facts are unique and stay."
)


def reconcile_briefs(*, role: str = "learner", max_facts: int = 400) -> dict:
    """CROSS-brief semantic cleanup: an LLM finds duplicate/contradictory facts
    that scattered across different scope briefs (the compaction consolidate only
    dedupes WITHIN a brief) and removes the redundant/stale copies, keeping one
    canonical version in the broadest scope. Feasible only at a bounded fact
    count (skips above ``max_facts`` so it stays a single call). Gated on
    ``AIFORGE_OKR_SCOPE_LLM``; ``AIFORGE_OKR_RECONCILE=0`` disables. Never raises."""
    # OPT-IN (default OFF): an LLM removing facts across briefs unsupervised can
    # be over-aggressive (it dropped ~24% on a stress run) and is inconsistent —
    # too risky for the automatic pipeline. Enable AIFORGE_OKR_RECONCILE=1 to run
    # it (manually or in recompact) when you want an aggressive cross-scope pass.
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0" \
            or os.environ.get("AIFORGE_OKR_RECONCILE", "0") != "1":
        return {"removed": 0, "skipped": "disabled"}
    from aiforge_core.runtime import work_notes
    briefs: dict = {}          # key -> [facts]
    total = 0
    for p in iter_briefs():
        if _CAPTURE_SIG_RE.search(p.name):
            continue
        key = p.stem[len("compacted-"):]
        try:
            facts = work_notes.parse_note(
                p.read_text(encoding="utf-8"))["sections"].get("facts") or []
        except OSError:
            continue
        if facts:
            briefs[key] = facts
            total += len(facts)
    if total < 2 or total > max_facts:
        return {"removed": 0, "skipped": f"facts={total}"}

    listing = "\n".join(f"{k} :: {_fact_body(f)}"
                        for k, fs in briefs.items() for f in fs)
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class _Rm(BaseModel):
            scope: str = ""
            fact: str = ""

        class _Removes(BaseModel):
            removes: list[_Rm] = []

        res = structured_complete(
            role,
            [{"role": "system", "content": _RECONCILE_SYS},
             {"role": "user", "content": listing[:24000]}],
            _Removes, max_tokens=2000, max_retries=1, temperature=0.0)
        removes = getattr(res, "removes", None) or []
    except Exception as exc:  # noqa: BLE001
        _log.debug("reconcile_briefs LLM failed: %s", exc)
        return {"removed": 0, "error": "llm_unreachable"}

    # group removals by scope key → ci-keys to drop
    drop: dict = {}
    for r in removes:
        k = (getattr(r, "scope", "") or "").strip()
        f = (getattr(r, "fact", "") or "").strip()
        if k in briefs and f:
            drop.setdefault(k, set()).add(work_notes._ci_key(f))
    removed = 0
    with _WRITE_LOCK:
        for k, dks in drop.items():
            p = brief_path(k)
            try:
                parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            kept = [f for f in facts
                    if work_notes._ci_key(_fact_body(f)) not in dks]
            if len(kept) != len(facts):
                removed += len(facts) - len(kept)
                work_notes.update_note(str(p), facts=kept, kind="knowledge", key=k)
    return {"removed": removed, "scopes": len(drop)}


def dedupe_global_copies() -> dict:
    """Remove facts from project/topic briefs when the SAME fact (case-insensitive)
    already lives in the global ``compacted-shared.md`` brief. Recall always
    unions the global brief for every scope, so those copies are pure redundancy
    — dropping them de-duplicates the md layer without any recall loss. Fresh
    read-modify-write under ``_WRITE_LOCK``. Never raises."""
    from aiforge_core.runtime import work_notes
    shared = brief_path("shared")
    if not shared.is_file():
        return {"removed": 0, "briefs": 0}
    try:
        gfacts = work_notes.parse_note(
            shared.read_text(encoding="utf-8"))["sections"].get("facts") or []
    except OSError:
        return {"removed": 0, "briefs": 0}
    gkeys = {work_notes._ci_key(_fact_body(f)) for f in gfacts}
    if not gkeys:
        return {"removed": 0, "briefs": 0}
    removed = 0
    touched = 0
    with _WRITE_LOCK:
        for p in iter_briefs():
            if p.name == "compacted-shared.md" or _CAPTURE_SIG_RE.search(p.name):
                continue
            try:
                parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            facts = parsed["sections"].get("facts") or []
            kept = [f for f in facts
                    if work_notes._ci_key(_fact_body(f)) not in gkeys]
            if len(kept) != len(facts):
                removed += len(facts) - len(kept)
                touched += 1
                work_notes.update_note(str(p), facts=kept, kind="knowledge",
                                       key=p.stem[len("compacted-"):])
    return {"removed": removed, "briefs": touched}


def cleanup_reheal(*, role: str = "learner") -> dict:
    """Recovery for an over-aggressive reheal: re-classify each moved
    (``source: reheal``) fact ON ITS OWN and DELETE the ones that are not truly
    global — strip them from ``compacted-shared.md`` and remove their capture
    files. Origin repo was not recorded, so a non-global fact is removed, not
    restored to its project. Run BEFORE any recompaction reword the shared facts
    (matching is verbatim). Gated on ``AIFORGE_OKR_SCOPE_LLM``. Never raises."""
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return {"checked": 0, "removed": 0, "skipped": "llm_off"}
    from aiforge_core.runtime import work_notes
    reheal: list[dict] = []
    for p in memory_dir().glob("*.md"):
        if p.name.startswith("compacted-"):
            continue
        try:
            d = _parse(p)
        except Exception:  # noqa: BLE001
            continue
        if str(d.get("source") or "") != "reheal":
            continue
        head = (d.get("body") or "").strip().splitlines()
        fact = head[0].strip().lstrip("-* ").strip() if head else ""
        if fact:
            reheal.append({"path": p, "fact": fact})
    if not reheal:
        return {"checked": 0, "removed": 0}

    remove_keys: set[str] = set()
    remove_paths: list = []
    checked = 0
    for r in reheal:
        checked += 1
        try:
            sc = classify_scope(r["fact"], role=role)   # judged on its own merit
        except Exception:  # noqa: BLE001
            continue
        if sc["scope"] != "global":
            remove_keys.add(work_notes._ci_key(_fact_body(r["fact"])))
            remove_paths.append(r["path"])

    removed = 0
    matched: set[str] = set()
    if remove_keys:
        # Fresh read-modify-write under _WRITE_LOCK (concurrent captures to shared
        # also take _WRITE_LOCK — don't clobber them with a stale snapshot).
        with _WRITE_LOCK:
            shared = brief_path("shared")
            if shared.is_file():
                parsed = work_notes.parse_note(shared.read_text(encoding="utf-8"))
                facts = parsed["sections"].get("facts") or []
                kept = []
                for f in facts:
                    k = work_notes._ci_key(_fact_body(f))
                    if k in remove_keys:
                        matched.add(k)
                        removed += 1
                    else:
                        kept.append(f)
                if removed:
                    work_notes.update_note(str(shared), facts=kept,
                                           kind="knowledge", key="shared")
    # P2 shortfall guard: only delete a capture file whose fact was actually
    # found + removed from shared. A flagged fact NOT found (the shared brief was
    # reworded by a compaction since reheal) keeps its capture file, so the
    # recovery source isn't lost while an orphan remains in the brief.
    deleted = 0
    orphaned = 0
    for r in reheal:
        _rk = work_notes._ci_key(_fact_body(r["fact"]))
        if _rk not in remove_keys:
            continue                              # stays global — keep
        if _rk in matched:
            try:
                r["path"].unlink()
                deleted += 1
            except OSError:
                pass
        else:
            orphaned += 1
    if orphaned:
        _log.warning("cleanup_reheal: %d flagged fact(s) not found verbatim in "
                     "shared (reworded by a compaction?) — capture files kept "
                     "for recovery; run BEFORE a recompact next time", orphaned)
    return {"checked": checked, "removed": removed,
            "capture_files_deleted": deleted, "orphaned": orphaned}


def capture(kind: str, text: str, *, repo: str | None = None,
            topic: str | None = None, title: str | None = None,
            source: str = "capture", tags: list[str] | None = None,
            ingest: bool = True, classify: bool = True) -> dict:
    """Persist one captured item as an md memory (repo + topic stamped + tagged),
    so it flows into both compaction axes. ``kind`` should be one of
    ``_CAPTURE_KINDS`` (falls back to a plain note otherwise). Returns the parsed
    md, or ``{"skipped": ...}`` for empty text."""
    text = (text or "").strip()
    if not text:
        return {"skipped": "empty"}
    k = kind if kind in _CAPTURE_KINDS else "note"
    # Scope decision: a repo-hinted fact that is actually cross-project gets
    # PROMOTED to the shared (global) brief. Promotion-only — never demote a
    # global capture into a repo — so the deterministic/off path (which returns
    # "project" for any repo hint) leaves existing behaviour untouched. A caller
    # that already resolved the scope (e.g. session compaction) passes
    # ``classify=False`` to avoid a second LLM call.
    if repo and classify:
        try:
            if classify_scope(text, hint_repo=repo,
                              hint_topic=topic)["scope"] == "global":
                repo, topic = None, None
        except Exception:  # noqa: BLE001 — scope upkeep never breaks a write
            pass
    tset = list(tags or [])
    if repo:
        tset.append(f"repo:{_slug(repo)}")
    if topic:
        tset.append(f"topic:{_slug(topic)}")
    tset.append(k)
    ttl = title or (text.splitlines()[0][:70] if text else k)
    res = write(ttl, text, kind=k, tags=list(dict.fromkeys(tset)),
                source=source, repo=repo or "shared", topic=topic, ingest=ingest)
    # WRITE-TIME brief maintenance: fold the fact into the repo's compacted brief
    # RIGHT NOW (cheap, no LLM), so recall (which reads compacted-<repo>.md) sees
    # just-written data instead of waiting for the periodic compaction. Periodic
    # compaction later only RE-SUMMARIZES to bound size.
    # Global writes (no repo) maintain the SHARED brief (compacted-shared.md),
    # which _project_brief unions into every context — so global memory is
    # compacted + surfaced the same as a repo's/ticket's.
    try:
        _brief_upsert(repo or "shared", text, topic=topic)
    except Exception:  # noqa: BLE001 — brief upkeep never breaks a write
        pass
    return res


def sweep_stale_captures(*, archive: bool = True) -> dict:
    """Retire per-run capture files that MASQUERADE as canonical briefs.

    A capture is stamped ``<slug>-YYYYMMDD-<6hex>.md``. When its title happened
    to start with "compacted" (e.g. the legacy-cleanup re-writing a brief's own
    title) the slug became ``compacted-…`` — and ``compact()`` excludes every
    ``compacted-*`` file from its live set, so these transient captures slip
    past compaction FOREVER and accumulate (``compacted-retry-on-empty-fix`` &
    friends). Their facts are already folded into the real
    ``compacted-<topic>.md`` brief by ``_brief_upsert`` at write time, so they
    carry nothing new.

    Moves each masquerader into ``archive/<ts>/`` (reversible; ``archive=False``
    deletes). Canonical briefs — ``compacted-<topic>.md`` with NO date-hex
    suffix — are untouched. Runs in the hourly compaction. Never raises."""
    import shutil
    sig = re.compile(r"-\d{8}-[0-9a-f]{6}\.md$")   # per-run capture signature
    swept: list[str] = []
    dst = memory_dir() / "archive" / _now_iso().replace(":", "")
    try:
        with _COMPACT_LOCK:
            for p in iter_briefs():
                if not sig.search(p.name):
                    continue                    # real canonical brief — keep
                try:
                    if archive:
                        dst.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(p), str(dst / p.name))
                    else:
                        p.unlink()
                    swept.append(p.name)
                except OSError:
                    continue
    except Exception as exc:  # noqa: BLE001 — sweep is best-effort upkeep
        return {"ok": False, "error": str(exc), "swept": len(swept)}
    return {"ok": True, "swept": len(swept), "archived": archive,
            "files": swept}


def sweep_empty_briefs(*, archive: bool = True) -> dict:
    """Retire DEAD canonical briefs — a ``compacted-<key>.md`` that carries only
    the boilerplate Objective with NO Facts, Key results, Learnings, or body.

    These accumulate when a topic's facts all migrate into another brief (the
    labeller re-clusters), when a fact-only brief is emptied, or from legacy
    ``compacted-compacted-*`` double-fold artifacts — leaving a stub that shows
    up as an "empty" memory but holds no knowledge. Moves each into
    ``archive/<ts>/`` (reversible; ``archive=False`` deletes). A brief with ANY
    real content is never touched. Never raises."""
    import shutil

    from aiforge_core.runtime import work_notes
    sig = re.compile(r"-\d{8}-[0-9a-f]{6}\.md$")   # skip per-run captures
    swept: list[str] = []
    dst = memory_dir() / "archive" / _now_iso().replace(":", "")
    try:
        with _COMPACT_LOCK:
            for p in iter_briefs():
                if sig.search(p.name):
                    continue                        # capture — sweep_stale owns it
                try:
                    parsed = work_notes.parse_note(
                        p.read_text(encoding="utf-8", errors="replace"))
                except Exception:  # noqa: BLE001
                    continue
                sec = parsed.get("sections") or {}
                # objective is ALWAYS the boilerplate line — a brief is "dead"
                # only when it has no Facts / Key results / Learnings / Links /
                # body. Links matter: map_scopes links are BIDIRECTIONAL, so
                # deleting a links-only brief orphans its sibling's inbound link.
                if (sec.get("facts") or sec.get("learnings")
                        or sec.get("key_results") or sec.get("links")
                        or (parsed.get("body") or "").strip()):
                    continue                        # has real content — keep
                try:
                    if archive:
                        dst.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(p), str(dst / p.name))
                    else:
                        p.unlink()
                    swept.append(p.name)
                except OSError:
                    continue
    except Exception as exc:  # noqa: BLE001 — best-effort upkeep
        return {"ok": False, "error": str(exc), "swept": len(swept)}
    return {"ok": True, "swept": len(swept), "archived": archive, "files": swept}


_BRIEF_CAP = 24_000   # chars; periodic re-summarize keeps it below this

# Knowledge briefs share the same Google-OKR envelope as the managed work
# notes (work_notes): Objective + Facts (write-time inbox, deduped) +
# Learnings + free body (the LLM-consolidated prose). One standard, whether
# the note is a Jira dossier or a memory brief.
_BRIEF_OBJECTIVE = ("Keep durable, deduped knowledge for {key} current — "
                    "write-time facts land here, periodic compaction folds "
                    "them into the consolidated body below.")
# LEGACY brief tail — old-format briefs kept fresh facts under "## Recent";
# migrated into the OKR "## Facts" section on first touch.
_LEGACY_RECENT_RE = re.compile(
    r"(?:^|\n)##\s+Recent\s*\n((?:\s*[-*]\s+.*\n?)*)", re.IGNORECASE)


def _render_brief(key: str, *, facts: list[str], body_md: str = "",
                  learnings: list[str] | None = None, title: str = "",
                  tags: list[str] | None = None,
                  key_results: list[str] | None = None) -> str:
    from aiforge_core.runtime import work_notes
    return work_notes.render_note(
        "knowledge", key,
        title=title or f"{key} memory (compacted)",
        objective=_BRIEF_OBJECTIVE.format(key=key),
        facts=facts, key_results=key_results, learnings=learnings,
        body_md=body_md, tags=tags)


def _parse_brief(raw: str) -> dict:
    """Parse a brief (OKR or legacy) → {"facts", "learnings", "body", "title"}.
    A legacy brief's ``## Recent`` bullets migrate into facts; its prose stays
    in body. Never raises."""
    from aiforge_core.runtime import work_notes
    parsed = work_notes.parse_note(raw or "")
    facts = list(parsed["sections"].get("facts") or [])
    body = parsed["body"] or ""
    m = _LEGACY_RECENT_RE.search(body)
    if m:
        facts.extend(re.sub(r"^[-*]\s+", "", ln.strip())
                     for ln in m.group(1).splitlines() if ln.strip())
        body = (body[:m.start()] + body[m.end():]).strip("\n")
    return {"facts": facts, "body": body, "title": parsed["title"],
            "learnings": list(parsed["sections"].get("learnings") or []),
            "key_results": list(parsed["sections"].get("key_results") or [])}


# A "key: value" fact whose key is a short label (status, owner, port, mode…) —
# a new value for the SAME key supersedes the stale one at write time.
_KEY_PREFIX_RE = re.compile(r"^([a-z][a-z0-9_ /-]{0,18}):\s+\S")
# A jira/issue key inside a fact → also a Key Result (the measurable work).
_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
# Prefixes that are ENCODINGS/STANDARDS/versions, not jira projects — a match
# here is a false ticket and must not seed a Key Result.
_TICKET_DENY = frozenset({
    "UTF", "SHA", "HTTP", "HTTPS", "ISO", "GPT", "AES", "RFC", "MD", "IPV",
    "IPV4", "IPV6", "COVID", "TLS", "SSL", "BASE", "X", "P", "T", "H", "K",
    "SO", "CVE", "PEP", "ES", "UI", "API"})
# Generic prose leaders ("note:", "todo:") are NOT supersede keys — two unrelated
# facts sharing one must not collide.
_KEY_DENY = frozenset({
    "note", "todo", "fix", "fixme", "warning", "warn", "error", "info",
    "update", "nb", "eg", "tip", "hint", "see", "also", "aside", "hack",
    "xxx", "caveat", "gotcha", "important", "reminder"})


def _fact_body(s: str) -> str:
    """Drop a leading ``[topic]`` prefix so comparisons hit the fact content."""
    return re.sub(r"^\[[^\]]*\]\s+", "", str(s or "")).strip()


def _brief_upsert(repo: str, text: str, *, topic: str | None = None) -> None:
    """Fold ``text`` into ``compacted-<repo>.md`` immediately (no LLM), as a
    deduped item under the OKR ``## Facts`` section. Creates the brief (OKR
    envelope) if absent; legacy-format briefs are migrated in place. Bounded:
    past ``_BRIEF_CAP`` the OLDEST facts are dropped (the periodic
    re-summarize folds them into the consolidated body anyway).

    Write-time hygiene (so recall doesn't see stale/duplicate facts before the
    next compaction): a new ``key: value`` supersedes the stale value for that
    key (W1); a new fact that CONTAINS an existing shorter one prunes the short
    (W6); a jira/issue key is seeded into ``## Key Results`` (W2)."""
    text = (text or "").strip()
    if not text:
        return
    slug = _slug(repo)
    path = brief_path(slug)
    fact = text.replace("\n", " ").strip()
    item = (f"[{topic}] " if topic else "") + fact
    with _WRITE_LOCK:
        facts: list[str] = []
        body = ""
        learnings: list[str] = []
        key_results: list[str] = []
        title = ""
        if path.exists():
            raw = path.read_text(encoding="utf-8", errors="replace")
            b = _parse_brief(raw)
            facts, body = b["facts"], b["body"]
            learnings, title = b["learnings"], b["title"]
            key_results = b["key_results"]
        # already captured (contained in an existing fact) or folded into prose
        if any(fact in _fact_body(f) for f in facts) or (fact and fact in body):
            return
        # W6: the new fact EXTENDS an existing shorter one → drop the short.
        # PREFIX-ANCHORED + length-gated, NOT bare substring — bare containment
        # silently deletes distinct/opposite facts ("retries 3x" swallowed by
        # "no retries 3x here") and short tokens ("auth" by "reauth…").
        # W1: a new `key: value` supersedes the stale value for the SAME key —
        # but generic prose leaders (note:/todo:) are excluded, and a key whose
        # value still holds a ':' is rejected (kills "note: the port: …" grabbing
        # the wrong key).
        kp = _KEY_PREFIX_RE.match(fact)
        new_key = kp.group(1).strip().lower() if kp else None
        if new_key and (new_key in _KEY_DENY
                        or ":" in fact.split(":", 1)[1]):
            new_key = None

        def _keep(f: str) -> bool:
            fb = _fact_body(f)
            # W6 prefix-extend prune — the new fact EXTENDS the old at a WORD
            # boundary (so "config set" is NOT pruned by "config setup").
            if (len(fb) >= 8 and fb != fact and fact.startswith(fb)
                    and fact[len(fb):len(fb) + 1] in ("", " ")):
                return False
            if new_key:                                # W1 supersede same key
                mm = _KEY_PREFIX_RE.match(fb)
                if mm and mm.group(1).strip().lower() == new_key:
                    return False
            return True

        dropped = [f for f in facts if not _keep(f)]
        facts = [f for f in facts if _keep(f)]
        facts.append(item)
        # Reconcile the search index: a fact superseded/pruned from the brief
        # must also leave the index, else recall keeps surfacing the stale value
        # until the next dedupe sweep (audit STORING HIGH-1).
        for _df in dropped:
            _dfb = _fact_body(_df)
            if len(_dfb) < 12:
                continue        # too short → a substring match would over-delete
            try:
                from aiforge_core.memory import backend_select, sqlite_memory
                if backend_select.embedded():
                    sqlite_memory.delete_by_text_contains(
                        _dfb, repo=slug, exclude_kind="knowledge")
            except Exception:  # noqa: BLE001
                pass
        # W2: seed a jira/issue key into Key Results (the measurable work) —
        # skipping encoding/standard tokens (UTF-8, SHA-256) and deduping on a
        # word boundary so ABC-12 isn't masked by an existing ABC-123.
        for tk in _TICKET_RE.findall(fact):
            if tk.split("-", 1)[0] in _TICKET_DENY:
                continue
            if not any(re.search(rf"\b{re.escape(tk)}\b", k) for k in key_results):
                key_results.append(fact if len(fact) <= 140 else tk)
            break
        # bound: drop OLDEST facts first (consolidated body is the keeper)
        while len(facts) > 1 and \
                (len(body) + sum(len(f) + 3 for f in facts)) > _BRIEF_CAP:
            facts.pop(0)
        path.write_text(
            _render_brief(repo, facts=facts, body_md=body, learnings=learnings,
                          title=title, key_results=key_results),
            encoding="utf-8")


def migrate_to_okr() -> dict:
    """One-shot: rewrite every knowledge BRIEF (``compacted-<scope>.md``) that
    is still in the legacy shape (``# heading`` + ``## Recent`` bullets, or a
    plain ``kind: compacted`` prose file) into the standard OKR envelope.

    Idempotent — a brief already in OKR form (``kind: knowledge``) is skipped.
    Only touches ``compacted-*.md`` (the memory briefs); per-session notes,
    rule books and skills keep their own formats. Returns
    ``{"ok", "migrated", "skipped", "files"}``; never raises."""
    migrated: list[str] = []
    skipped = 0
    for p in iter_briefs():
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _FM_RE.match(raw)
        fm_block = m.group(1) if m else ""
        if re.search(r'^\s*kind:\s*"?knowledge"?\s*$', fm_block, re.MULTILINE):
            skipped += 1
            continue
        # scope key = the part after "compacted-" in the filename
        key = p.stem[len("compacted-"):] or "shared"
        b = _parse_brief(raw)
        with _WRITE_LOCK:
            try:
                p.write_text(
                    _render_brief(key, facts=b["facts"], body_md=b["body"],
                                  learnings=b["learnings"], title=b["title"]),
                    encoding="utf-8")
            except OSError:
                continue
        migrated.append(p.name)
    return {"ok": True, "migrated": len(migrated), "skipped": skipped,
            "files": migrated}


def _find_by_source(source: str) -> Path | None:
    """Locate the md file whose frontmatter ``source`` matches (the stable
    key for a session), so repeated runs UPDATE one file instead of
    spawning a new hashed file every time."""
    for p in _all_md_files():
        try:
            if _parse(p).get("source") == source:
                return p
        except Exception:  # noqa: BLE001
            continue
    return None


def upsert_section(*, source: str, title: str, section_title: str,
                   section_body: str, kind: str = "session",
                   tags: list[str] | None = None, repo: str = "notes") -> dict:
    """Append a section to the file keyed by ``source`` (create on first
    use). The filename is the FULL readable ``title`` slug — one stable
    file per session that grows with each run, then re-ingested whole.
    """
    tags = list(tags or [])
    with _WRITE_LOCK:
        existing = _find_by_source(source)
        if existing is not None:
            raw = existing.read_text(encoding="utf-8", errors="replace").rstrip()
            existing.write_text(
                raw + f"\n\n## {section_title}\n\n{section_body.strip()}\n",
                encoding="utf-8")
            path = existing
        else:
            stem = _slug(title)
            path = _md_path_for_stem(stem)
            i = 1
            while path.exists():       # different session, same title → suffix
                path = _md_path_for_stem(f"{stem}-{i}")
                i += 1
            fm = (
                "---\n"
                f"title: {title}\n"
                f"kind: {kind}\n"
                f"tags: {', '.join(tags)}\n"
                f"source: {source}\n"
                f"created: {_now_iso()}\n"
                "---\n\n"
            )
            path.write_text(fm + f"## {section_title}\n\n{section_body.strip()}\n",
                            encoding="utf-8")
    d = _parse(path)
    _ingest_unit(title=d["title"], body=d["body"], kind=kind,
                 tags=d["tags"] or tags, source=source, repo=repo)
    d.pop("body", None)
    return d


def append_bullet(*, source: str, title: str, bullet: str,
                  kind: str = "rule", tags: list[str] | None = None,
                  repo: str = "rules") -> dict:
    """Append a deduped ``- bullet`` to the file keyed by ``source`` (one
    clean list, e.g. a rule book). Used for user rules that must persist
    and apply every session."""
    tags = list(tags or [])
    line = "- " + bullet.strip()
    with _WRITE_LOCK:
        existing = _find_by_source(source)
        if existing is not None:
            raw = existing.read_text(encoding="utf-8", errors="replace").rstrip()
            # Dedup against existing BULLET LINES (exact), not a raw substring
            # — a short bullet that's a substring of a longer one is not a dup.
            if line in raw.splitlines():
                d = _parse(existing); d.pop("body", None); return d
            existing.write_text(raw + "\n" + line + "\n", encoding="utf-8")
            path = existing
        else:
            stem = _slug(title)
            path = _md_path_for_stem(stem)
            i = 1
            while path.exists():
                path = _md_path_for_stem(f"{stem}-{i}")
                i += 1
            fm = (
                "---\n"
                f"title: {title}\n"
                f"kind: {kind}\n"
                f"tags: {', '.join(tags)}\n"
                f"source: {source}\n"
                f"created: {_now_iso()}\n"
                "---\n\n"
            )
            path.write_text(fm + line + "\n", encoding="utf-8")
    d = _parse(path)
    _ingest_unit(title=d["title"], body=d["body"], kind=kind,
                 tags=d["tags"] or tags, source=source, repo=repo)
    d.pop("body", None)
    return d


def ingest_dir() -> dict:
    """(Re)ingest every md file in the memory dir into the backend.

    For files dropped in by hand. Dedup is handled by the backend's own
    content hashing, so re-running is safe.
    """
    n = 0
    present_sources: set[str] = set()   # sources of files on disk → for reconcile
    # Reclaim compacted-brief rows stranded under repo='notes' before briefs were
    # ingested under their real scope (else they linger as duplicates forever).
    try:
        from aiforge_core.memory import backend_select as _bsel
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
            _purged = _sqlmem.delete_stale_compacted_notes()
            if _purged:
                _log.info("ingest_dir: purged %d stale repo=notes brief rows", _purged)
    except Exception:  # noqa: BLE001
        pass
    for p in _all_md_files():
        try:
            d = _parse(p)
            body, repo, replace = d["body"], "notes", False
            kind, source = d["kind"], f"md:{p.stem}"
            if p.stem.startswith("compacted-"):
                # a consolidated brief: ingest EXACTLY as compact()'s Phase-3
                # does — same kind ("compacted") AND source ("compacted:<stem>")
                # — so the two ingest paths reclaim ONE row instead of storing
                # the same brief twice (once kind=knowledge/md:, once
                # kind=compacted/compacted:). Scope = repo / 'shared' / topic
                # (best-effort from the name), envelope stripped.
                base = p.stem[len("compacted-"):]
                # Only strip a `-N` split-part suffix when the primary
                # compacted-<base>.md exists — else a real slug ending in a
                # number (log4j-2, s3-bucket-1) would be mangled to the wrong key.
                m = re.match(r"^(.*)-\d+$", base)
                if m and (_resolve_md("compacted-" + m.group(1)) is not None):
                    base = m.group(1)
                repo = base or "notes"
                # kind = the brief's REAL kind ('knowledge'), not the mechanical
                # 'compacted' (that showed up as the label in search/UI); a clean
                # human title, not the compacted-<key> stem. Source stays
                # compacted:<stem> so the two ingest paths still reclaim one row.
                kind, source = "knowledge", f"compacted:{p.stem}"
                d = {**d, "title": _brief_title(base)}
                replace = True                      # reclaim the prior brief row
                try:
                    from aiforge_core.runtime import work_notes
                    body = work_notes.knowledge_text(d["body"])
                except Exception:  # noqa: BLE001
                    pass
            else:
                repo = d.get("repo") or "notes"
            _ingest_unit(title=d["title"], body=body, kind=kind,
                         tags=d["tags"], source=source, repo=repo,
                         replace=replace)
            present_sources.add(source)
            n += 1
        except Exception:  # noqa: BLE001
            continue
    # RECONCILE: md is the source of truth — prune index rows whose md file was
    # DELETED or archived (create/update already handled by the ingest above).
    pruned = 0
    try:
        from aiforge_core.memory import backend_select as _bsel
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
            pruned = _sqlmem.prune_missing_file_rows(present_sources)
            if pruned:
                _log.info("ingest_dir: pruned %d orphan rows (md deleted)", pruned)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "ingested": n, "pruned": pruned,
            "dir": str(memory_dir())}


def _demote_headings(body: str, by: int = 2) -> str:
    """Push every markdown heading in ``body`` ``by`` levels deeper (capped at
    h6) so an embedded ``# Title`` doesn't collide with the ``##`` section
    wrapper a compacted file gives each source note."""
    out = []
    for line in body.splitlines():
        m = re.match(r"^(#{1,6})(\s)", line)
        if m:
            lvl = min(6, len(m.group(1)) + by)
            out.append("#" * lvl + line[len(m.group(1)):])
        else:
            out.append(line)
    return "\n".join(out)


_SUMMARY_SYS = (
    "You consolidate engineering memory notes. Merge the notes below into ONE "
    "concise markdown document. Deduplicate ruthlessly, group related points "
    "under '## ' section headings, and KEEP every concrete fact, decision, "
    "gotcha, file path, command, id and number. Drop chit-chat, repetition and "
    "filler. Do not invent anything. Output ONLY the markdown body — no preamble, "
    "no surrounding code fence."
)
_SUMMARY_INPUT_CAP = 28_000     # chars of notes per LLM call (map-reduce above)
_COMPACT_BODY_CAP = 60_000      # max chars of a deterministic-merge consolidated
                                # file (bounds growth when no model is reachable)


def _summarize_block(text: str, role: str) -> str | None:
    """One LLM consolidation call. Returns markdown, or None on any failure
    (model down / unknown role / empty) so the caller falls back to merge."""
    try:
        from aiforge_core.llm.client import complete
        out = complete(
            role,
            [{"role": "system", "content": _SUMMARY_SYS},
             {"role": "user", "content": text}],
            temperature=0.2, max_tokens=4096,
        )
    except Exception:  # noqa: BLE001 — any failure → deterministic merge
        return None
    out = (out or "").strip()
    # strip an accidental wrapping ```/```md fence
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z0-9]*\s*\n?", "", out)
        out = re.sub(r"\n?```\s*$", "", out).strip()
    return out or None


def _summarize_notes(blocks: list[str], role: str) -> str | None:
    """Map-reduce consolidation: summarize the notes (batched to fit the input
    cap), then summarize the partial summaries if there was more than one
    batch. Returns markdown or None (→ caller merges deterministically)."""
    if not blocks:
        return None
    # Greedily batch blocks under the input cap.
    batches: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for b in blocks:
        if cur and cur_len + len(b) > _SUMMARY_INPUT_CAP:
            batches.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(b)
        cur_len += len(b)
    if cur:
        batches.append("\n\n".join(cur))

    partials: list[str] = []
    for batch in batches:
        s = _summarize_block(batch, role)
        if s is None:
            return None          # bail whole op → deterministic merge
        partials.append(s)
    if len(partials) == 1:
        return partials[0]
    # reduce step — combine the partial summaries
    combined = "\n\n---\n\n".join(partials)
    if len(combined) <= _SUMMARY_INPUT_CAP:
        return _summarize_block(combined, role) or combined
    return combined          # already summarized; accept as-is if still huge


def _topic_labels(files: list[dict], role: str) -> dict:
    """Ask an LLM to cluster notes into a few COHERENT topics (better than one
    blob per kind). Returns {file_name: topic-slug}. Empty on any failure — the
    caller then falls back to kind grouping. Cheap: one call over titles only."""
    if len(files) < 2:
        return {}
    listing = "\n".join(f"{i}: {(d.get('title') or d.get('file') or '')[:80]}"
                        for i, d in enumerate(files))
    try:
        from pydantic import RootModel

        from aiforge_core.llm.structured import structured_complete

        class _Topics(RootModel[dict]):
            pass

        raw = structured_complete(role, [
            {"role": "system", "content":
             "Cluster these memory-note titles into 3-10 COHERENT topics (by "
             "subject/feature area). Reply ONLY a JSON object mapping each index "
             "(as a string) to a short kebab-case topic slug, e.g. "
             '{"0":"data-sync","1":"chat-ui"}. Every index must appear once.'},
            {"role": "user", "content": listing[:6000]},
        ], _Topics, max_tokens=800, max_retries=1, temperature=0.0).root
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    import re as _re
    labels: dict = {}
    for k, v in raw.items():
        try:
            idx = int(k)
        except (ValueError, TypeError):
            continue
        if 0 <= idx < len(files) and isinstance(v, str) and v.strip():
            slug = _re.sub(r"[^a-z0-9]+", "-", v.strip().lower()).strip("-")[:40]
            if slug:
                labels[files[idx]["file"]] = slug
    return labels


def _group_key(d: dict, group_by: str) -> str:
    if group_by == "repo":
        # Project-brief axis: one consolidated file per repo. An explicit
        # frontmatter `repo`, else a `repo:<x>` tag, else "shared" (cross-repo).
        if d.get("repo"):
            return d["repo"]
        for t in d.get("tags") or []:
            if t.startswith("repo:"):
                return t.split(":", 1)[1] or "shared"
        return "shared"
    if group_by == "topic":
        # Topic axis: explicit frontmatter `topic` or a `topic:<slug>` tag wins
        # (no LLM needed); else the precomputed label; else kind.
        if d.get("topic"):
            return d["topic"]
        for t in d.get("tags") or []:
            if t.startswith("topic:"):
                return t.split(":", 1)[1] or (d.get("kind") or "note")
        return d.get("_topic") or (d.get("kind") or "note")
    if group_by == "tag":
        return (d["tags"][0] if d.get("tags") else "untagged")
    if group_by == "source":
        # the leading token of the source key (e.g. "chat", "md", "ticket")
        return (d.get("source") or "manual").split(":", 1)[0].split("-", 1)[0]
    return d.get("kind") or "note"


def _topic_split_cap() -> int:
    """Facts-size (chars) beyond which a topic brief SPLITS into linked parts.
    A major topic that outgrows this becomes compacted-<topic>.md +
    compacted-<topic>-2.md … cross-referenced. Env AIFORGE_TOPIC_SPLIT_CAP."""
    try:
        return max(500, int(os.environ.get("AIFORGE_TOPIC_SPLIT_CAP", "12000")))
    except (TypeError, ValueError):
        return 12000


def _brief_parts(key: str, sections: dict, tags, title: str) -> list[tuple[str, str]]:
    """Render an OKR knowledge brief → ``[(stem, content), …]``. Facts are paged
    under the split cap: a topic that fits is ONE file; a topic that outgrows it
    splits into compacted-<key>.md + compacted-<key>-2.md … each carrying the
    OKR envelope (kind/tags/objective) and a cross-reference back to part 1 /
    forward to the next (the "split and refer" pattern). Key Results + Learnings
    stay on part 1 (the canonical head)."""
    from aiforge_core.runtime import work_notes
    facts = [str(f) for f in (sections.get("facts") or [])]
    kr = sections.get("key_results") or []
    links = sections.get("links") or []
    learnings = sections.get("learnings") or []
    cap = _topic_split_cap()
    pages: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for f in facts:
        if cur and size + len(f) + 3 > cap:
            pages.append(cur)
            cur, size = [], 0
        cur.append(f)
        size += len(f) + 3
    if cur:
        pages.append(cur)
    pages = pages or [[]]
    n = len(pages)
    base = _slug(key)
    parts: list[tuple[str, str]] = []
    for i, page in enumerate(pages):
        part_key = key if i == 0 else f"{key}-{i + 1}"
        stem = f"compacted-{base}" if i == 0 else f"compacted-{base}-{i + 1}"
        xref: list[str] = []
        if n > 1:
            if i > 0:
                xref.append(f"**Part {i + 1} of {n}** · main topic: "
                            f"[{base}](compacted-{base}.md)")
            if i < n - 1:
                xref.append(f"**Continued in:** "
                            f"[part {i + 2}](compacted-{base}-{i + 2}.md)")
        content = work_notes.render_note(
            "knowledge", part_key,
            title=(title if n == 1 else f"{title} (part {i + 1}/{n})"),
            objective=_BRIEF_OBJECTIVE.format(key=key),
            key_results=(kr if i == 0 else None),
            facts=page, links=links,
            learnings=(learnings if i == 0 else None),
            tags=tags, body_md="\n\n".join(xref))
        parts.append((stem, content))
    return parts


def _union_back(new_list, old_list) -> list:
    """Recover items from ``old_list`` missing from ``new_list`` so an LLM fold
    can't silently DROP curated content (Learnings / Key Results / Links). The
    recovered (older) items are PREPENDED, keeping ``new_list`` (the LLM's
    current view / the chronological tail) LAST — so a downstream ``[-N:]``
    recency cap still selects the newest, not the resurrected old ones."""
    new = list(new_list or [])
    missing = [x for x in (old_list or []) if x not in new]
    return missing + new


def _consolidate_brief_sections(key: str, path, blocks: list[str],
                                model_role: str, tags) -> tuple[dict, list]:
    """LLM-consolidate the group into OKR sections (dedupe/map/supersede via
    work_notes.consolidate) and return ``(sections, merged_tags)``. Prior
    hand-added Learnings + the brief's prior tags are preserved."""
    from aiforge_core.runtime import work_notes
    # Read the primary brief AND every split-out part (compacted-<key>-N.md) so a
    # re-fold NEVER loses facts that a previous oversize split moved into part 2+.
    existing: dict = {"facts": [], "learnings": [], "links": [], "key_results": [],
                      "objective": ""}
    prev_tags: list = []
    base = _slug(key)
    part_paths = [path] + _brief_part_paths(base)
    for pp in part_paths:
        if not pp.exists():
            continue
        parsed = work_notes.parse_note(pp.read_text(encoding="utf-8", errors="replace"))
        sec = parsed["sections"]
        existing["objective"] = existing["objective"] or (sec.get("objective") or "")
        for fld in ("facts", "learnings", "links", "key_results"):
            for it in sec.get(fld) or []:
                if it not in existing[fld]:
                    existing[fld].append(it)
        prev_tags += list((parsed["frontmatter"] or {}).get("tags") or [])
    new_content = "\n\n".join(b for b in blocks if b.strip())
    # RE-FOLD a fact-only brief. Force compaction adds every existing brief as an
    # empty-live group; a brief that carries Facts but no consolidated PROSE body
    # yields blocks=[] → new_content="" → consolidate() takes its no-LLM
    # "nothing new" path and the force pass does zero real work (270 briefs in 8s,
    # no model calls). Feed the brief's existing Facts back as content so the LLM
    # genuinely re-consolidates (dedupe/supersede/re-map) them. Only fires when
    # there is no new content, i.e. exactly the force re-fold case — normal
    # compaction always has live items, so new_content is non-empty and this is
    # a no-op there.
    if not new_content.strip() and existing.get("facts"):
        new_content = "\n".join(f"- {f}" for f in existing["facts"])
    merged = work_notes.consolidate(existing, new_content, role=model_role)
    # Deterministic UNION-BACK of derived/curated sections the LLM might omit:
    # Learnings (audit trail), Key Results (write-time W2 tickets) and Links
    # (map_scopes sibling links). Without this a single fold that drops them
    # loses that content permanently on the daily recompact.
    merged["learnings"] = _union_back(merged.get("learnings"),
                                      existing.get("learnings"))
    merged["key_results"] = _union_back(merged.get("key_results"),
                                        existing.get("key_results"))
    merged["links"] = _union_back(merged.get("links"), existing.get("links"))
    return merged, list(prev_tags) + list(tags or [])


def _consolidate_brief_content(key: str, path, blocks: list[str], title: str,
                               model_role: str,
                               tags: list[str] | None = None) -> str:
    """Build an OKR knowledge brief by LLM-consolidating this group's notes.

    Folds ``blocks`` (the group's units + any prior consolidated body) into the
    prior brief's OKR sections via ``work_notes.consolidate`` — dedupe
    paraphrases, resolve contradictions (newer supersedes), MAP each item to
    Objective/Key Results/Facts/Links/Learnings; chonkie chunks large input.
    Prior hand-added Learnings (the audit trail) are unioned back in so the LLM
    can never drop them. consolidate() degrades to a deterministic union+dedupe
    when no model is reachable, so this never loses content."""
    from aiforge_core.runtime import work_notes
    existing: dict = {}
    prev_tags: list = []
    if path.exists():
        _parsed = work_notes.parse_note(
            path.read_text(encoding="utf-8", errors="replace"))
        existing = _parsed["sections"]
        prev_tags = list((_parsed["frontmatter"] or {}).get("tags") or [])
    new_content = "\n\n".join(b for b in blocks if b.strip())
    merged = work_notes.consolidate(existing, new_content, role=model_role)
    # Union-back the derived/curated sections the LLM might drop (see
    # _consolidate_brief_sections): Learnings, Key Results, Links.
    learnings = _union_back(merged.get("learnings"), existing.get("learnings"))
    key_results = _union_back(merged.get("key_results"),
                              existing.get("key_results"))
    links = _union_back(merged.get("links"), existing.get("links"))
    # union the group's tags with the brief's prior tags (render normalizes/dedupes)
    all_tags = list(prev_tags) + list(tags or [])
    return work_notes.render_note(
        "knowledge", key, title=title,
        objective=_BRIEF_OBJECTIVE.format(key=key),
        key_results=key_results, facts=merged.get("facts"),
        links=links, learnings=learnings, body_md="", tags=all_tags)


def compact(*, group_by: str = "kind", min_group: int = 2,
            dry_run: bool = False, summarize: bool = True,
            model_role: str = "learner", archive_sources: bool = True,
            force: bool = False, progress=None) -> dict:
    """Consolidate the sprawl of per-session ``.md`` memories into ONE
    standardized file per group, so the Memory folder stays legible.

    Grouping key (``group_by``): ``kind`` (default), ``tag``, or ``source``.
    Only groups with at least ``min_group`` files are compacted; singletons
    are left alone.

    ``summarize`` (default True): an available LLM (``model_role``'s primary →
    cloud chain) rewrites each group into a deduplicated, concise document, so
    the consolidated file stays SMALL instead of growing every run. On a
    re-compact the existing consolidated body is fed back in and re-summarised,
    keeping size bounded. If no model is reachable (or ``summarize=False``) it
    falls back to a deterministic merge (one ``## <title>`` section per note,
    appended). Originals are MOVED into ``<memory>/archive/<ts>/`` (reversible —
    never deleted) and the result is re-ingested into the searchable backend.

    ``dry_run`` returns the plan (group → file count) without touching disk.

    ``force`` ("compact at any cost"): re-consolidate EVERY existing brief too —
    not just scopes with new files. Each brief is re-read, re-chunked (chonkie)
    and re-summarised by the LLM from scratch, and singletons always fold
    (min_group→1, summarize→on). Use to rebuild the whole memory after a bad
    import, or to re-run the LLM pass over everything.
    """
    import shutil
    if force:
        summarize = True
        min_group = 1

    def _gather_planned() -> dict[str, list[dict]]:
        files: list[dict] = []
        for p in memory_dir().glob("*.md"):
            try:
                d = _parse(p)
                d["_path"] = p
                files.append(d)
            except Exception:  # noqa: BLE001
                continue
        live = [d for d in files if not d["file"].startswith("compacted-")]
        # The REPO brief is a curated project-learning projection — keep raw
        # per-session transcripts (kind="session") out of it. But the TOPIC axis
        # is exactly where sessions belong: memory organized BY TOPIC. Large
        # transcripts are fine here now — consolidate() distils them via the LLM
        # (chonkie chunks big input) into Facts/Learnings rather than dumping,
        # and the raw file archives out after folding. Excluding them was why
        # per-session memory lingered and compaction said "nothing to compact".
        if group_by == "repo":
            live = [d for d in live if (d.get("kind") or "") != "session"]
        # TOPIC mode: one LLM pass labels every note with a coherent topic slug so
        # compaction yields several browsable topical files instead of ONE blob
        # per kind. Falls back to kind grouping for any note the labeller missed
        # (or all, if the model is unreachable).
        if group_by == "topic":
            try:
                _labels = _topic_labels(live, model_role)
            except Exception:  # noqa: BLE001
                _labels = {}
            for d in live:
                d["_topic"] = _labels.get(d["file"])
        groups: dict[str, list[dict]] = {}
        for d in live:
            groups.setdefault(_group_key(d, group_by), []).append(d)
        result = {k: v for k, v in groups.items() if len(v) >= min_group}
        if force:
            # re-consolidate every EXISTING brief too (recheck all files) — add
            # each compacted-<scope>.md as its own group so the loop re-reads +
            # re-summarises it even with no new live sources. Skip split-part /
            # per-run-named files (they fold via their primary scope).
            for p in iter_briefs():
                if re.search(r"-\d{8}-[0-9a-f]{6}$", p.stem):
                    continue
                key = p.stem[len("compacted-"):] or "shared"
                result.setdefault(key, [])       # empty live → existing_body re-consolidated
        return result

    if dry_run:                      # read-only preview — no lock (don't wait
        planned = _gather_planned()  # behind a long-running compaction)
        return {
            "ok": True, "dry_run": True, "group_by": group_by,
            "groups": {k: len(v) for k, v in sorted(planned.items())},
            "files_in": sum(len(v) for v in planned.values()),
            "files_out": len(planned),
        }

    out_files: list[str] = []
    summarized_files: list[str] = []
    moved = 0

    # Serialize compactions against each other so two concurrent runs can't
    # read the same stale consolidated state and clobber each other. This lock
    # is held across the (slow) summarise, but it is NOT _WRITE_LOCK, so it does
    # NOT block ordinary chat-turn memory writes — only other compactions wait.
    with _COMPACT_LOCK:
        # Gather INSIDE the lock so a second compaction sees the first's result
        # (fresh sources + the just-written consolidated file as existing_body).
        planned = _gather_planned()
        if not planned:
            return {"ok": True, "dry_run": False, "group_by": group_by,
                    "groups": {}, "files_in": 0, "files_out": 0,
                    "note": "nothing to compact (no group ≥ min_group)"}

        archive = memory_dir() / "archive" / _now_iso().replace(":", "")

        # ── Phase 1: build each group's body (LLM summarise; no _WRITE_LOCK
        # so concurrent chat-turn writes aren't frozen during the slow call) ──
        prepared: list[dict] = []
        _ptotal = len(planned)
        _will_llm = summarize and group_by in ("repo", "topic")
        _log.info("compact[%s]: %d brief(s) to fold%s", group_by, _ptotal,
                  " via LLM" if _will_llm else " (deterministic)")
        for _pi, (key, items) in enumerate(sorted(planned.items()), 1):
            if progress:
                try:
                    progress(_pi, _ptotal, key)
                except Exception:  # noqa: BLE001
                    pass
            _log.info("compact[%s]: [%d/%d] folding '%s' (%d file%s)…",
                      group_by, _pi, _ptotal, key, len(items),
                      "" if len(items) == 1 else "s")
            items.sort(key=lambda d: d.get("created") or "")
            all_tags = sorted({t for d in items for t in d.get("tags") or []})
            title = f"{key.replace('-', ' ').strip().capitalize()} memory (compacted)"
            stem = f"compacted-{_slug(key)}"
            path = _md_path_for_stem(stem)

            # Existing consolidated body (re-compaction) — fed back so it gets
            # RE-SUMMARISED with the new notes, keeping the file bounded.
            # For the knowledge axes (repo/topic) the previous file is an OKR
            # envelope: parse it so ONLY the prior consolidated PROSE (not the
            # Objective/Facts head or the sentinel) is re-fed — otherwise the
            # envelope text would nest inside the new body every compaction.
            existing_body = ""
            if path.exists():
                prev = path.read_text(encoding="utf-8", errors="replace")
                if group_by in ("repo", "topic"):
                    existing_body = _parse_brief(prev)["body"].strip()
                else:
                    pm = _FM_RE.match(prev)
                    existing_body = (pm.group(2).strip() if pm else prev.strip())

            sections, blocks = [], []
            if existing_body:
                blocks.append("### (previous consolidated)\n\n" + existing_body)
            for d in items:
                meta = (f"_source: {d.get('source') or 'manual'} · "
                        f"created: {d.get('created') or '?'}_")
                sections.append(
                    f"## {d['title']}\n\n{meta}\n\n"
                    f"{_demote_headings(d['body']).strip()}".rstrip())
                blocks.append(f"### {d['title']}\n\n{d['body'].strip()}")
            merged_prefix = (existing_body + "\n\n---\n\n") if existing_body \
                else f"# {title}\n\n"
            merged_body = merged_prefix + "\n\n---\n\n".join(sections)

            # Knowledge axes (repo/topic) with a model → STRUCTURED consolidation
            # into real OKR sections (Facts/Links/Learnings), via
            # work_notes.consolidate (dedupe / map / supersede; chonkie chunks
            # big input). The prose-summary + deterministic-merge paths below
            # stay for the kind axis and for the no-model (summarize=False) case.
            _use_structured = (group_by in ("repo", "topic")) and summarize

            body = None
            did_summarize = False
            if summarize and not _use_structured:
                summary = _summarize_notes(blocks, model_role)   # SLOW
                if summary:
                    body = f"# {title}\n\n{summary}"
                    did_summarize = True
            if body is None and not _use_structured:
                body = merged_body
                # Bound the deterministic-merge fallback so an always-down model
                # can't grow the file every run (the "file too big" problem).
                if len(body) > _COMPACT_BODY_CAP:
                    head = f"# {title}\n\n"
                    keep = max(1000, _COMPACT_BODY_CAP - len(head) - 80)
                    body = (head + "_…older entries trimmed (kept in archive/); "
                            "configure a model so compaction can summarise._\n\n"
                            "---\n\n" + body[-keep:])

            if _use_structured:
                # LLM folds the group into structured OKR sections, then Facts
                # are paged: a topic that outgrows the split cap becomes several
                # cross-referenced parts. The raw units archive out (scheduler),
                # so the topic note(s) ARE the memory.
                merged, all_tags = _consolidate_brief_sections(
                    key, path, blocks, model_role, all_tags)
                part_list = _brief_parts(key, merged, all_tags, title)
                did_summarize = True
            elif group_by in ("repo", "topic"):
                # No model: keep the OKR envelope, consolidation lives in the body
                # (Facts reset — they were folded in); Learnings survive verbatim.
                prev_learnings = _parse_brief(
                    path.read_text(encoding="utf-8", errors="replace")
                )["learnings"] if path.exists() else []
                part_list = [(stem, _render_brief(
                    key, facts=[],
                    body_md=re.sub(r"^#\s[^\n]*\n+", "", body.strip()),
                    learnings=prev_learnings, title=title, tags=all_tags))]
            else:
                fm = (
                    "---\n"
                    f"title: {title}\n"
                    "kind: compacted\n"
                    f"tags: {', '.join(all_tags)}\n"
                    f"source: compacted:{stem}\n"
                    f"created: {_now_iso()}\n"
                    f"count: {len(items)}\n"
                    f"summarized: {str(did_summarize).lower()}\n"
                    "---\n\n"
                )
                part_list = [(stem, fm + body.strip() + "\n")]
            prepared.append({"items": items, "base_stem": stem, "key": key,
                             "parts": part_list, "tags": all_tags,
                             "summarized": did_summarize})

        # ── Phase 2: write consolidated, THEN archive originals, UNDER lock.
        # Write-before-move: if a write fails, the originals stay in place
        # (no data loss) rather than being archived with no consolidated file.
        with _WRITE_LOCK:
            archive.mkdir(parents=True, exist_ok=True)
            for p in prepared:
                new_stems = {st for st, _ in p["parts"]}
                # Retire STALE split overflow: prior parts of this topic that the
                # new (smaller) fold no longer produces — archive them so a topic
                # that shrank doesn't leave orphaned compacted-<key>-N.md files.
                _base = p["base_stem"][len("compacted-"):] \
                    if p["base_stem"].startswith("compacted-") else p["base_stem"]
                for old in _brief_part_paths(_base):
                    if old.stem not in new_stems and re.match(
                            rf"^{re.escape(p['base_stem'])}-\d+$", old.stem):
                        try:
                            shutil.move(str(old), str(archive / old.name))
                        except Exception:  # noqa: BLE001
                            pass
                wrote_any = False
                for st, content in p["parts"]:
                    fpath = _md_path_for_stem(st)
                    try:
                        fpath.write_text(content, encoding="utf-8")
                    except Exception:  # noqa: BLE001 — keep originals; skip
                        continue
                    out_files.append(fpath.name)
                    wrote_any = True
                    if p["summarized"]:
                        summarized_files.append(fpath.name)
                if not wrote_any or not archive_sources:
                    continue    # projection mode keeps raw units for the OTHER
                                # axis (a unit feeds both its repo + topic brief).
                for d in p["items"]:
                    try:
                        shutil.move(str(d["_path"]), str(archive / d["file"]))
                        moved += 1
                    except Exception:  # noqa: BLE001
                        pass

        # ── Phase 3: re-ingest into the search backend ──────────────────
        for p in prepared:
            for st, _ in p["parts"]:
                fpath = _md_path_for_stem(st)
                if fpath.name not in out_files or not fpath.exists():
                    continue                   # write failed → don't ingest
                try:
                    doc = _parse(fpath)
                    ingest_body = doc["body"]
                    # Knowledge briefs (repo/topic) are OKR envelopes — ingest
                    # ONLY the knowledge (Facts + body) so recall vectors don't
                    # carry the identical Objective boilerplate every brief has.
                    if group_by in ("repo", "topic"):
                        try:
                            from aiforge_core.runtime import work_notes
                            ingest_body = work_notes.knowledge_text(doc["body"])
                        except Exception:  # noqa: BLE001
                            pass
                    # Ingest the brief under its REAL scope so recall can reach
                    # it: a project brief → its repo; the shared brief →
                    # 'shared' (global, surfaced for every repo query); a topic
                    # brief → NULL (repo-agnostic, globally visible). Burying
                    # every brief under 'notes' (the old default) made all
                    # consolidated OKR knowledge invisible to repo-scoped recall.
                    _bkey = p.get("key")
                    _brepo = ((None if group_by == "topic" else _bkey)
                              if _bkey else "notes")
                    # real kind ('knowledge') + clean human title (see ingest_dir)
                    _ingest_unit(title=_brief_title(_bkey or st), body=ingest_body,
                                 kind="knowledge", tags=p["tags"],
                                 source=f"compacted:{st}", repo=_brepo,
                                 replace=True)
                except Exception:  # noqa: BLE001
                    pass

    return {
        "ok": True, "dry_run": False, "group_by": group_by,
        "groups": {k: len(v) for k, v in sorted(planned.items())},
        "files_in": moved, "files_out": len(out_files),
        "compacted": out_files, "summarized": summarized_files,
        "archive": str(archive),
    }


# Compacted files whose KEY is not a real topic — id-keyed briefs (chat run in
# a jira/confluence context / session scratch produced these) and per-kind
# blobs. Their knowledge is re-captured as topic units then the file archived,
# so a topic compaction re-folds them into meaningful topic briefs.
_CRYPTIC_KEY_RE = re.compile(
    r"^(?:\d{4,}|[a-z]{2,5}-\d+|session-\d+|"
    r"session|project|project-learning|learning|chat-summary|notes|compacted)$",
    re.IGNORECASE)


def cleanup_legacy_compacted(*, dry_run: bool = False,
                             model_role: str = "learner",
                             refold: bool = True, progress=None) -> dict:
    """One-time tidy: fold id-keyed / per-kind ``compacted-*`` briefs back into
    the TOPIC axis. Each stale file's Facts are re-captured as topic units (no
    forced topic → the labeller re-clusters them), the original is archived
    (reversible), then a topic compaction re-folds everything into meaningful,
    tagged, split-aware topic briefs. ``dry_run`` reports the plan only."""
    import shutil

    from aiforge_core.runtime import work_notes
    stale: list = []
    for pth in iter_briefs():
        base = pth.stem[len("compacted-"):]
        # BUG ARTIFACT: a compacted-* file that is NOT a proper kind=knowledge
        # brief (e.g. a stray kind=note unit written under a compacted name, or
        # a source that starts 'agent:') — fold + archive it; the real topic
        # brief is regenerated on the next compaction.
        try:
            fm = work_notes.parse_note(
                pth.read_text(encoding="utf-8", errors="replace"))["frontmatter"]
        except Exception:  # noqa: BLE001
            fm = {}
        if (fm.get("kind") and fm.get("kind") != "knowledge") \
                or str(fm.get("source") or "").startswith("agent:"):
            stale.append(pth)
            continue
        # A split overflow part (compacted-<topic>-N) is NOT stale when its
        # topic's primary brief exists and that topic name isn't itself cryptic
        # — e.g. keep compacted-auth-2.md (topic 'auth'), but still flag a truly
        # cryptic compacted-clr-3049.md (no compacted-clr.md primary).
        mnum = re.match(r"^(.*)-\d+$", base)
        if mnum and not _CRYPTIC_KEY_RE.match(mnum.group(1)) \
                and (_resolve_md("compacted-" + mnum.group(1)) is not None):
            continue
        if _CRYPTIC_KEY_RE.match(base):
            stale.append(pth)
    if dry_run:
        return {"ok": True, "dry_run": True,
                "stale": sorted(p.name for p in stale), "count": len(stale)}
    if not stale:
        _log.info("tidy-legacy: no cryptic/id-named briefs to fold")
        return {"ok": True, "dry_run": False, "folded": 0, "facts": 0,
                "note": "no id-keyed / per-kind compacted files to clean"}
    _log.info("tidy-legacy: folding %d cryptic/id-named brief(s)%s",
              len(stale), " + re-compacting" if refold else "")
    archive = memory_dir() / "archive" / ("cleanup-" + _now_iso().replace(":", ""))
    facts_moved = 0
    folded = 0
    with _COMPACT_LOCK:
        archive.mkdir(parents=True, exist_ok=True)
        for pth in stale:
            try:
                parsed = work_notes.parse_note(
                    pth.read_text(encoding="utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
            facts = list(parsed["sections"].get("facts") or [])
            # legacy per-kind blobs keep knowledge in the body, not Facts
            if not facts:
                body_know = work_notes.knowledge_text(
                    pth.read_text(encoding="utf-8", errors="replace"))
                facts = [ln.lstrip("-* ").strip()
                         for ln in body_know.splitlines()
                         if ln.strip() and not ln.startswith("#")][:200]
            for f in facts:
                if f.strip():
                    try:
                        capture("topic_learning", f.strip(), repo="notes",
                                source="cleanup:legacy-compacted")
                        facts_moved += 1
                    except Exception:  # noqa: BLE001
                        pass
            try:
                shutil.move(str(pth), str(archive / pth.name))
                folded += 1
            except Exception:  # noqa: BLE001
                pass
    # Re-fold the re-captured units into meaningful topic briefs — SKIP when the
    # caller (e.g. 'compact all') runs its own topic pass right after, so the
    # heavy LLM consolidation isn't done twice.
    topic = None
    if refold:
        topic = compact(group_by="topic", min_group=1, summarize=True,
                        model_role=model_role, archive_sources=True,
                        progress=progress)
    return {"ok": True, "dry_run": False, "folded": folded,
            "facts": facts_moved, "archive": str(archive),
            "topic_compact": topic}


def delete_file(name: str) -> bool:
    p = _resolve_md(name)
    if p and p.is_file():
        stem = p.stem
        p.unlink()
        # Sync the vector index: drop this file's row(s) immediately (md is the
        # source of truth — a deleted file must not linger in search).
        try:
            from aiforge_core.memory import backend_select, sqlite_memory
            if backend_select.embedded():
                src = (f"compacted:{stem}" if stem.startswith("compacted-")
                       else f"md:{stem}")
                sqlite_memory.delete_by_source(src)
        except Exception:  # noqa: BLE001
            pass
        return True
    return False
