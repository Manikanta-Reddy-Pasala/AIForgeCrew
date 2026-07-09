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
                meta[k.strip()] = v.strip()
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


def _brief_upsert(repo: str, text: str, *, topic: str | None = None) -> None:
    """Append ``text`` as a bullet into ``compacted-<repo>.md`` immediately (no
    LLM), deduped by content. Creates the brief if absent. Bounded: when it
    exceeds ``_BRIEF_CAP`` the OLDEST bullets are dropped (the periodic
    re-summarize pass reconstitutes a tight version)."""
    text = (text or "").strip()
    if not text:
        return
    slug = _slug(repo)
    path = memory_dir() / f"compacted-{slug}.md"
    heading = f"# {repo} memory (compacted)"
    head = (f"---\ntitle: {repo} memory (compacted)\n"
            f"kind: compacted\nrepo: {repo}\nsource: brief:{slug}\n---\n\n")
    fact = text.replace("\n", " ").strip()
    new_bullet = "- " + (f"[{topic}] " if topic else "") + fact
    with _WRITE_LOCK:
        # PRESERVE the existing body (a periodic re-summarize writes PROSE, not
        # bullets — extracting only bullets would clobber it). Append the new
        # fact under a "## Recent" tail; dedup if the fact is already present
        # anywhere (prose or bullet), so we never re-add summarized content.
        if path.exists():
            raw = path.read_text(encoding="utf-8", errors="replace")
            m = _FM_RE.match(raw)
            body = (m.group(2).rstrip() if m else raw.rstrip())
        else:
            body = heading
        if fact and fact in body:
            return
        if "## Recent" not in body:
            body = body + "\n\n## Recent"
        body = body + "\n" + new_bullet
        # bound: keep the heading + summarized top, trim the OLDEST "## Recent"
        # bullets (the periodic re-summarize folds them into the prose anyway).
        if len(body) > _BRIEF_CAP:
            lines = body.splitlines()
            recent_at = next((i for i, ln in enumerate(lines)
                              if ln.strip() == "## Recent"), None)
            if recent_at is not None:
                top = lines[:recent_at + 1]
                bl = lines[recent_at + 1:]
                while bl and len("\n".join(top + bl)) > _BRIEF_CAP:
                    bl.pop(0)
                body = "\n".join(top + bl)
            else:
                body = body[-_BRIEF_CAP:]
        path.write_text(head + body + "\n", encoding="utf-8")


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
        # The repo/topic briefs are LEARNING projections — exclude raw session
        # transcripts (kind="session"), which are large and belong to the
        # session-summary compaction, not a project/topic brief.
        if group_by in ("repo", "topic"):
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
            existing_body = ""
            if path.exists():
                prev = path.read_text(encoding="utf-8", errors="replace")
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

            body = None
            did_summarize = False
            if summarize:
                summary = _summarize_notes(blocks, model_role)   # SLOW
                if summary:
                    body = f"# {title}\n\n{summary}"
                    did_summarize = True
            if body is None:
                body = merged_body
                # Bound the deterministic-merge fallback so an always-down model
                # can't grow the file every run (the "file too big" problem).
                if len(body) > _COMPACT_BODY_CAP:
                    head = f"# {title}\n\n"
                    keep = max(1000, _COMPACT_BODY_CAP - len(head) - 80)
                    body = (head + "_…older entries trimmed (kept in archive/); "
                            "configure a model so compaction can summarise._\n\n"
                            "---\n\n" + body[-keep:])

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
            prepared.append({"items": items, "path": path, "stem": stem,
                             "tags": all_tags, "content": fm + body.strip() + "\n",
                             "summarized": did_summarize})

        # ── Phase 2: write consolidated, THEN archive originals, UNDER lock.
        # Write-before-move: if a write fails, the originals stay in place
        # (no data loss) rather than being archived with no consolidated file.
        with _WRITE_LOCK:
            archive.mkdir(parents=True, exist_ok=True)
            for p in prepared:
                try:
                    p["path"].write_text(p["content"], encoding="utf-8")
                except Exception:  # noqa: BLE001 — keep originals; skip this group
                    continue
                out_files.append(p["path"].name)
                if p["summarized"]:
                    summarized_files.append(p["path"].name)
                if not archive_sources:
                    continue    # projection mode: keep raw units for the OTHER
                                # axis (a unit belongs to BOTH its repo brief and
                                # its topic note) — briefs are derived views.
                for d in p["items"]:
                    try:
                        shutil.move(str(d["_path"]), str(archive / d["file"]))
                        moved += 1
                    except Exception:  # noqa: BLE001
                        pass

        # ── Phase 3: re-ingest into the search backend ──────────────────
        for p in prepared:
            if p["path"].name not in out_files:
                continue                       # write failed → don't ingest
            try:
                doc = _parse(p["path"])
                _ingest_unit(title=doc["title"], body=doc["body"],
                             kind="compacted", tags=p["tags"],
                             source=f"compacted:{p['stem']}", repo="notes")
            except Exception:  # noqa: BLE001
                pass

    return {
        "ok": True, "dry_run": False, "group_by": group_by,
        "groups": {k: len(v) for k, v in sorted(planned.items())},
        "files_in": moved, "files_out": len(out_files),
        "compacted": out_files, "summarized": summarized_files,
        "archive": str(archive),
    }


def delete_file(name: str) -> bool:
    p = memory_dir() / (name if name.endswith(".md") else f"{name}.md")
    if p.is_file() and p.parent == memory_dir():
        p.unlink()
        return True
    return False
