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
import os
import re
import threading
from pathlib import Path

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
    for p in sorted(memory_dir().glob("*.md"), reverse=True):
        try:
            d = _parse(p)
            d.pop("body", None)
            d["mtime"] = p.stat().st_mtime
            out.append(d)
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda d: d.get("created") or "", reverse=True)
    return out


def read_file(name: str) -> dict | None:
    p = memory_dir() / (name if name.endswith(".md") else f"{name}.md")
    if not p.is_file() or p.parent != memory_dir():
        return None
    return _parse(p)


def _ingest_unit(*, title: str, body: str, kind: str, tags: list[str],
                 source: str, repo: str) -> None:
    """Mirror a note into the active memory backend so it's searchable."""
    text = f"{title}\n\n{body}".strip()
    from aiforge_core.memory import backend_select as _bsel
    try:
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
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
    stamped here simply won't roll up into either brief."""
    tags = list(tags or [])
    created = _now_iso()
    digest = hashlib.sha1((title + text).encode()).hexdigest()[:6]
    stem = f"{_slug(title)}-{created[:10].replace('-', '')}-{digest}"
    path = memory_dir() / f"{stem}.md"
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


def capture(kind: str, text: str, *, repo: str | None = None,
            topic: str | None = None, title: str | None = None,
            source: str = "capture", tags: list[str] | None = None,
            ingest: bool = True) -> dict:
    """Persist one captured item as an md memory (repo + topic stamped + tagged),
    so it flows into both compaction axes. ``kind`` should be one of
    ``_CAPTURE_KINDS`` (falls back to a plain note otherwise). Returns the parsed
    md, or ``{"skipped": ...}`` for empty text."""
    text = (text or "").strip()
    if not text:
        return {"skipped": "empty"}
    k = kind if kind in _CAPTURE_KINDS else "note"
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
                  tags: list[str] | None = None) -> str:
    from aiforge_core.runtime import work_notes
    return work_notes.render_note(
        "knowledge", key,
        title=title or f"{key} memory (compacted)",
        objective=_BRIEF_OBJECTIVE.format(key=key),
        facts=facts, learnings=learnings, body_md=body_md, tags=tags)


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
            "learnings": list(parsed["sections"].get("learnings") or [])}


def _brief_upsert(repo: str, text: str, *, topic: str | None = None) -> None:
    """Fold ``text`` into ``compacted-<repo>.md`` immediately (no LLM), as a
    deduped item under the OKR ``## Facts`` section. Creates the brief (OKR
    envelope) if absent; legacy-format briefs are migrated in place. Bounded:
    past ``_BRIEF_CAP`` the OLDEST facts are dropped (the periodic
    re-summarize folds them into the consolidated body anyway)."""
    text = (text or "").strip()
    if not text:
        return
    slug = _slug(repo)
    path = memory_dir() / f"compacted-{slug}.md"
    fact = text.replace("\n", " ").strip()
    item = (f"[{topic}] " if topic else "") + fact
    with _WRITE_LOCK:
        facts: list[str] = []
        body = ""
        learnings: list[str] = []
        title = ""
        if path.exists():
            raw = path.read_text(encoding="utf-8", errors="replace")
            b = _parse_brief(raw)
            facts, body = b["facts"], b["body"]
            learnings, title = b["learnings"], b["title"]
        # dedupe: already captured as a fact (any topic), or folded into prose
        if any(fact in f for f in facts) or (fact and fact in body):
            return
        facts.append(item)
        # bound: drop OLDEST facts first (consolidated body is the keeper)
        while len(facts) > 1 and \
                (len(body) + sum(len(f) + 3 for f in facts)) > _BRIEF_CAP:
            facts.pop(0)
        path.write_text(
            _render_brief(repo, facts=facts, body_md=body,
                          learnings=learnings, title=title),
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
    for p in sorted(memory_dir().glob("compacted-*.md")):
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
    for p in memory_dir().glob("*.md"):
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
            path = memory_dir() / f"{stem}.md"
            i = 1
            while path.exists():       # different session, same title → suffix
                path = memory_dir() / f"{stem}-{i}.md"
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
            path = memory_dir() / f"{stem}.md"
            i = 1
            while path.exists():
                path = memory_dir() / f"{stem}-{i}.md"
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
    for p in memory_dir().glob("*.md"):
        try:
            d = _parse(p)
            _ingest_unit(title=d["title"], body=d["body"], kind=d["kind"],
                         tags=d["tags"], source=f"md:{p.stem}", repo="notes")
            n += 1
        except Exception:  # noqa: BLE001
            continue
    return {"ok": True, "ingested": n, "dir": str(memory_dir())}


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
    part_paths = [path] + sorted(memory_dir().glob(f"compacted-{base}-*.md"))
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
    merged = work_notes.consolidate(existing, new_content, role=model_role)
    learnings = list(merged.get("learnings") or [])
    for ln in (existing.get("learnings") or []):        # keep the audit trail
        if ln not in learnings:
            learnings.append(ln)
    merged["learnings"] = learnings
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
    learnings = list(merged.get("learnings") or [])
    for ln in (existing.get("learnings") or []):        # never lose the audit trail
        if ln not in learnings:
            learnings.append(ln)
    # union the group's tags with the brief's prior tags (render normalizes/dedupes)
    all_tags = list(prev_tags) + list(tags or [])
    return work_notes.render_note(
        "knowledge", key, title=title,
        objective=_BRIEF_OBJECTIVE.format(key=key),
        key_results=merged.get("key_results"), facts=merged.get("facts"),
        links=merged.get("links"), learnings=learnings, body_md="",
        tags=all_tags)


def compact(*, group_by: str = "kind", min_group: int = 2,
            dry_run: bool = False, summarize: bool = True,
            model_role: str = "learner", archive_sources: bool = True) -> dict:
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
    """
    import shutil

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
        return {k: v for k, v in groups.items() if len(v) >= min_group}

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
        for key, items in sorted(planned.items()):
            items.sort(key=lambda d: d.get("created") or "")
            all_tags = sorted({t for d in items for t in d.get("tags") or []})
            title = f"{key.replace('-', ' ').strip().capitalize()} memory (compacted)"
            stem = f"compacted-{_slug(key)}"
            path = memory_dir() / f"{stem}.md"

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
            prepared.append({"items": items, "base_stem": stem,
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
                for old in memory_dir().glob(f"{p['base_stem']}-*.md"):
                    if old.stem not in new_stems and re.match(
                            rf"^{re.escape(p['base_stem'])}-\d+$", old.stem):
                        try:
                            shutil.move(str(old), str(archive / old.name))
                        except Exception:  # noqa: BLE001
                            pass
                wrote_any = False
                for st, content in p["parts"]:
                    fpath = memory_dir() / f"{st}.md"
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
                fpath = memory_dir() / f"{st}.md"
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
                    _ingest_unit(title=doc["title"], body=ingest_body,
                                 kind="compacted", tags=p["tags"],
                                 source=f"compacted:{st}", repo="notes")
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
                             model_role: str = "learner") -> dict:
    """One-time tidy: fold id-keyed / per-kind ``compacted-*`` briefs back into
    the TOPIC axis. Each stale file's Facts are re-captured as topic units (no
    forced topic → the labeller re-clusters them), the original is archived
    (reversible), then a topic compaction re-folds everything into meaningful,
    tagged, split-aware topic briefs. ``dry_run`` reports the plan only."""
    import shutil

    from aiforge_core.runtime import work_notes
    stale: list = []
    for pth in memory_dir().glob("compacted-*.md"):
        base = pth.stem[len("compacted-"):]
        # A split overflow part (compacted-<topic>-N) is NOT stale when its
        # topic's primary brief exists and that topic name isn't itself cryptic
        # — e.g. keep compacted-auth-2.md (topic 'auth'), but still flag a truly
        # cryptic compacted-clr-3049.md (no compacted-clr.md primary).
        mnum = re.match(r"^(.*)-\d+$", base)
        if mnum and not _CRYPTIC_KEY_RE.match(mnum.group(1)) \
                and (memory_dir() / f"compacted-{mnum.group(1)}.md").exists():
            continue
        if _CRYPTIC_KEY_RE.match(base):
            stale.append(pth)
    if dry_run:
        return {"ok": True, "dry_run": True,
                "stale": sorted(p.name for p in stale), "count": len(stale)}
    if not stale:
        return {"ok": True, "dry_run": False, "folded": 0, "facts": 0,
                "note": "no id-keyed / per-kind compacted files to clean"}
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
    # Re-fold the re-captured units into meaningful topic briefs.
    topic = compact(group_by="topic", min_group=1, summarize=True,
                    model_role=model_role, archive_sources=True)
    return {"ok": True, "dry_run": False, "folded": folded,
            "facts": facts_moved, "archive": str(archive),
            "topic_compact": topic}


def delete_file(name: str) -> bool:
    p = memory_dir() / (name if name.endswith(".md") else f"{name}.md")
    if p.is_file() and p.parent == memory_dir():
        p.unlink()
        return True
    return False
