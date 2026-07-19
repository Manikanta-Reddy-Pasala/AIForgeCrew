"""md_store internals: knowledge-brief rendering/parsing (the OKR envelope),
write-time brief upsert + hygiene, brief-index / seed-memory helpers and the
`compacted-*` migration. Depends only on `_base`."""
from __future__ import annotations

import os
import re

from ._base import (
    _CAPTURE_SIG_RE,
    _FM_RE,
    _WRITE_LOCK,
    _brief_title,
    _slug,
    brief_path,
    iter_briefs,
)


def brief_index() -> list[dict]:
    """A compact table-of-contents of EVERY brief — ``[{key, title, snippet}]``
    (snippet = the brief's first fact). The "seed memory" that tells a model what
    concepts exist so it knows what to recall (the video's "amnesia" fix — a
    model never queries memory it doesn't know is there). Cheap (reads headers +
    one fact). Sorted by key. Never raises."""
    from aiforge_core.runtime import work_notes
    out: list[dict] = []
    for p in iter_briefs():
        if _CAPTURE_SIG_RE.search(p.name):
            continue
        key = p.stem[len("compacted-"):]
        try:
            parsed = work_notes.parse_note(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        facts = parsed["sections"].get("facts") or []
        snippet = _fact_body(facts[0])[:100] if facts else ""
        out.append({"key": key,
                    "title": (parsed.get("frontmatter") or {}).get("title")
                    or _brief_title(key),
                    "snippet": snippet})
    return sorted(out, key=lambda d: d["key"])


def seed_memory_block(*, max_briefs: int = 60) -> str:
    """Render :func:`brief_index` as a compact prompt block so a chat/agent turn
    knows what memory EXISTS to query. Empty string when no briefs. Bounded by
    ``max_briefs`` (AIFORGE_SEED_TOC_MAX). Gated by AIFORGE_SEED_TOC (default on)."""
    if os.environ.get("AIFORGE_SEED_TOC", "1") == "0":
        return ""
    try:
        cap = max(1, int(os.environ.get("AIFORGE_SEED_TOC_MAX", str(max_briefs))))
    except (TypeError, ValueError):
        cap = max_briefs
    idx = brief_index()
    if not idx:
        return ""
    lines = ["[memory index] briefs you can recall (ask memory_lookup for detail):"]
    for d in idx[:cap]:
        s = f"  - {d['key']}: {d['title']}"
        if d["snippet"]:
            s += f" — {d['snippet']}"
        lines.append(s)
    if len(idx) > cap:
        lines.append(f"  … +{len(idx) - cap} more")
    return "\n".join(lines)
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
                  key_results: list[str] | None = None,
                  links: list[str] | None = None,
                  sources: list[str] | None = None) -> str:
    from aiforge_core.runtime import work_notes
    return work_notes.render_note(
        "knowledge", key,
        title=title or f"{key} memory (compacted)",
        objective=_BRIEF_OBJECTIVE.format(key=key),
        facts=facts, key_results=key_results, learnings=learnings,
        links=links, body_md=body_md, tags=tags, sources=sources)


def _parse_brief(raw: str) -> dict:
    """Parse a brief (OKR or legacy) → {"facts", "learnings", "body", "title",
    "links", "key_results", "sources"}.
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
            "links": list(parsed["sections"].get("links") or []),
            "key_results": list(parsed["sections"].get("key_results") or []),
            "sources": _stems(
                (parsed.get("frontmatter") or {}).get("sources"))}


def _stems(values) -> list[str]:
    """Normalise a brief's ``sources:`` frontmatter to capture STEMS.

    Tolerant of a hand-written ``foo.md``; anything blank is dropped. Kept here
    because both the writer and the reader of provenance must agree on the
    spelling, and this is the file that owns the brief format.
    """
    out: list[str] = []
    for v in values or []:
        s = str(v or "").strip()
        if s.endswith(".md"):
            s = s[:-3]
        if s and s not in out:
            out.append(s)
    return out


def brief_source_stems() -> set[str]:
    """Every capture stem that some brief RECORDS having consumed.

    The provenance a non-leader needs before it may archive anything: a capture
    is only tidied away once a brief that claims it has arrived by sync. Briefs
    written before ``sources:`` existed claim nothing, so their captures are
    simply left in place — untidy, never lost.

    Deliberately does NOT swallow read errors: the caller must archive nothing
    when the covered set is uncertain, and a half-read set would look certain.
    """
    from aiforge_core.runtime import work_notes
    out: set[str] = set()
    for p in iter_briefs():
        parsed = work_notes.parse_note(
            p.read_text(encoding="utf-8", errors="replace"))
        out.update(_stems((parsed.get("frontmatter") or {}).get("sources")))
    return out


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


def _reconcile_dropped_index(dropped, repo: str) -> None:
    """Drop the search-index rows for facts removed from a brief so recall stops
    surfacing them before the next full reingest. Only for the embedded (SQLite)
    backend; per-fact rows are the raw learning captures (NOT the brief itself, so
    ``exclude_kind='knowledge'``). Skips <12-char bodies (substring over-delete)."""
    for _df in dropped or []:
        _dfb = _fact_body(_df)
        if len(_dfb) < 12:
            continue
        try:
            from aiforge_core.memory import backend_select, sqlite_memory
            if backend_select.embedded():
                sqlite_memory.delete_by_text_contains(
                    _dfb, repo=_slug(repo), exclude_kind="knowledge")
        except Exception:  # noqa: BLE001
            pass


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
        sources: list[str] = []
        title = ""
        if path.exists():
            raw = path.read_text(encoding="utf-8", errors="replace")
            b = _parse_brief(raw)
            facts, body = b["facts"], b["body"]
            learnings, title = b["learnings"], b["title"]
            key_results = b["key_results"]
            # Provenance survives a write-time fact fold: this path adds a fact,
            # it does not consume captures, so what the brief already claims
            # must stay claimed (a peer is waiting on it to tidy up).
            sources = b["sources"]
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
                          title=title, key_results=key_results,
                          sources=sources),
            encoding="utf-8")


def migrate_to_okr() -> dict:
    """One-shot: rewrite every knowledge BRIEF (``compacted-<scope>.md``) that
    is still in the legacy shape (``# heading`` + ``## Recent`` bullets, or a
    plain ``kind: compacted`` prose file) into the standard OKR envelope.

    Idempotent — a brief already in OKF/OKR form (``type: knowledge`` or the
    legacy ``kind: knowledge``) is skipped. Only touches ``compacted-*.md`` (the
    memory briefs); per-session notes, rule books and skills keep their own
    formats. Returns ``{"ok", "migrated", "skipped", "files"}``; never raises."""
    migrated: list[str] = []
    skipped = 0
    for p in iter_briefs():
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _FM_RE.match(raw)
        fm_block = m.group(1) if m else ""
        # OKF `type:` (current) or legacy `kind:` — either means already-enveloped.
        if re.search(r'^\s*(?:type|kind):\s*"?knowledge"?\s*$', fm_block,
                     re.MULTILINE):
            skipped += 1
            continue
        # scope key = the part after "compacted-" in the filename
        key = p.stem[len("compacted-"):] or "shared"
        b = _parse_brief(raw)
        with _WRITE_LOCK:
            try:
                p.write_text(
                    _render_brief(key, facts=b["facts"], body_md=b["body"],
                                  learnings=b["learnings"], title=b["title"],
                                  sources=b["sources"]),
                    encoding="utf-8")
            except OSError:
                continue
        migrated.append(p.name)
    return {"ok": True, "migrated": len(migrated), "skipped": skipped,
            "files": migrated}
