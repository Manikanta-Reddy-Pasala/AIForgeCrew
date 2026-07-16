"""Render / parse / validate / update the standard OKR note (no LLM)."""
from __future__ import annotations

import os
import re

from ._helpers import (
    _BODY_MARK,
    _FRONTMATTER_RE,
    _SECTION_KEYS,
    _as_items,
    _log,
    _mirror_aliases,
    _now_iso,
    _yaml_str,
)
from ._links import normalize_links, normalize_tags


# ── Structure repair (write-path self-heal, never rejects) ────────────────
# Junk that a model (or a bad legacy fold) can leak INTO a Facts/Key results/
# Learnings item: the body sentinel, a markdown heading, a bare section label,
# a rule separator, or the brief's own Objective boilerplate. These aren't
# knowledge — they're the envelope scaffolding read back as content (the exact
# bug class we hit before). Scrubbed at render time so it never reaches disk.
_LEAK_ITEM_RE = re.compile(
    r"""^(?:
          \#{1,6}\s                                  # markdown heading
        | \#{0,2}\s*(?:objective|key\s*results|facts|links|learnings)\s*:?\s*$
        | -{3,}\s*$ | —{2,}\s*$                       # rule / separator
    )""", re.IGNORECASE | re.VERBOSE)
# Known envelope-boilerplate fragments that must never sit in a list item.
_BOILERPLATE_SUBSTR = ("keep durable, deduped knowledge",)


def _is_leak_item(s: str) -> bool:
    t = (s or "").strip()
    if not t or _BODY_MARK in t:
        return True
    if _LEAK_ITEM_RE.match(t):
        return True
    low = t.lower()
    return any(b in low for b in _BOILERPLATE_SUBSTR)


def scrub_items(value) -> list[str]:
    """`_as_items` + drop envelope-scaffolding leaks (heading/sentinel/section
    label/separator/boilerplate). Repair, not reject: bad items vanish, good
    ones stay."""
    return [s for s in _as_items(value) if not _is_leak_item(s)]


# Kinds this repo mints notes for; a blank/None kind is repaired to "knowledge"
# (the memory-brief default) rather than writing a header with no identity.
_KNOWN_KINDS = frozenset({"jira", "confluence", "web", "repo", "knowledge",
                          "topic", "session", "compacted", "rule", "prefs",
                          "note", "dossier"})


def validate_note(text: str) -> tuple[bool, list[str]]:
    """Re-parse a rendered note and report structure issues (does NOT mutate).
    Used by the write path to log what it repaired and by tests to assert the
    contract. ``ok`` is True when no issues remain."""
    issues: list[str] = []
    parsed = parse_note(text or "")
    fm = parsed.get("frontmatter") or {}
    # OKF requires a non-empty `type:`; we additionally want `key` + a
    # `timestamp`. parse_note mirrors legacy aliases so an old file (kind/
    # updated_at) still satisfies these.
    for req in ("type", "key", "timestamp"):
        if not str(fm.get(req) or "").strip():
            issues.append(f"missing frontmatter '{req}'")
    if not str(parsed.get("title") or "").strip():
        issues.append("missing title")
    sec = parsed.get("sections") or {}
    body = (parsed.get("body") or "").strip()
    has_content = bool((sec.get("objective") or "").strip()
                       or sec.get("facts") or sec.get("key_results")
                       or sec.get("learnings") or body)
    if not has_content:
        issues.append("empty note (no Objective/Facts/KR/Learnings/body)")
    for fld in ("facts", "key_results", "learnings"):
        for it in sec.get(fld) or []:
            if _is_leak_item(it):
                issues.append(f"scaffolding leaked into {fld}: {it[:40]!r}")
    return (not issues), issues


def render_note(kind: str, key: str, *, title: str, source_url: str = "",
                objective: str = "", key_results=None, facts=None,
                links=None, learnings=None, body_md: str = "",
                updated_at: str = "", tags=None,
                description: str = "", resource: str = "",
                timestamp: str = "") -> str:
    """Render the standard note in **Open Knowledge Format (OKF v0.1)**: the
    frontmatter's required identity field is ``type:`` (from ``kind``), the
    resource URI is ``resource:`` (from ``resource``/``source_url``), the
    last-change stamp is ``timestamp:`` (from ``timestamp``/``updated_at``), and
    an optional one-line ``description:`` is emitted when given. Empty sections
    are skipped; ordering is fixed (frontmatter → title → Objective → Key
    Results → Facts → Links → Learnings → free body). The stamp is injectable
    for deterministic tests / read-modify-write; it defaults to now (UTC).
    ``tags`` land in the frontmatter (metadata, not a body section).

    Legacy aliases (``source_url``/``updated_at`` kwargs) are still accepted so
    callers need no change; the OUTPUT is always OKF names. :func:`parse_note`
    mirrors OKF↔legacy keys so old files and old readers keep working."""
    # Repair (never reject): a blank kind gets the memory-brief default so the
    # header always has an identity; unknown kinds pass through (repo mints new
    # ones over time — don't gate on an allow-list).
    kind = (str(kind or "").strip() or "knowledge")
    norm_links = normalize_links(links, kind, key)
    norm_tags = normalize_tags(tags)
    res = (resource or source_url or "").strip()
    ts = (timestamp or updated_at or _now_iso())
    fm = [
        "---",
        f"type: {_yaml_str(kind)}",
        f"key: {_yaml_str(key)}",
        f"resource: {_yaml_str(res)}",
        f"timestamp: {_yaml_str(ts)}",
    ]
    desc = (description or "").strip()
    if desc:
        fm.append(f"description: {_yaml_str(desc)}")
    if norm_tags:
        fm.append("tags:")
        fm.extend(f"  - {_yaml_str(t)}" for t in norm_tags)
    else:
        fm.append("tags: []")
    if norm_links:
        fm.append("links:")
        fm.extend(f"  - {_yaml_str(lk)}" for lk in norm_links)
    else:
        fm.append("links: []")
    fm.append("---")

    parts = ["\n".join(fm), f"# {str(title or key).strip()}"]
    obj = (objective or "").strip()
    if obj:
        parts.append("## Objective\n\n" + obj)
    # scrub_items drops envelope scaffolding (heading/sentinel/section-label/
    # boilerplate) that a model or bad fold leaked into a list section.
    for heading, items in (("Key Results", scrub_items(key_results)),
                           ("Facts", scrub_items(facts)),
                           ("Links", norm_links),
                           ("Learnings", scrub_items(learnings))):
        if items:
            parts.append(f"## {heading}\n\n"
                         + "\n".join(f"- {i}" for i in items))
    body = (body_md or "").strip("\n")
    if body:
        parts.append(_BODY_MARK + "\n\n" + body)
    text = "\n\n".join(parts) + "\n"
    # Safety net: re-parse and log anything the scrub couldn't fix (never
    # raises — write proceeds; the log surfaces a real structural regression).
    ok, issues = validate_note(text)
    if not ok:
        _log.warning("render_note[%s/%s] structure issues: %s", kind, key, issues)
    return text


def parse_note(text: str) -> dict:
    """Parse a note (tolerantly) into
    ``{"frontmatter", "title", "sections", "body"}``.

    - ``sections`` holds ONLY the known OKR sections (canonical keys:
      objective → str; key_results/facts/links/learnings → list of items).
    - Everything else — prose outside the known sections and any UNKNOWN
      ``## Heading`` blocks (hand-added by users) — lands in ``body`` in
      original order, so a read-modify-write round-trip preserves it.
    Never raises on a str input; a legacy/ad-hoc file simply parses as
    frontmatter={} with everything in body.
    """
    text = text or ""
    fm: dict = {}
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            import yaml
            loaded = yaml.safe_load(m.group(1))
            if isinstance(loaded, dict):
                fm = _mirror_aliases(loaded)
        except Exception:  # noqa: BLE001 — hand-edited YAML must not explode
            fm = {}
        text = text[m.end():]

    # The explicit body sentinel splits managed head from free body exactly.
    # Hand-made files without it fall through to the heuristic below (a
    # non-bullet line ends a list section).
    lines = text.splitlines()
    tail = ""
    for i, ln in enumerate(lines):
        if ln.strip() == _BODY_MARK:
            tail = "\n".join(lines[i + 1:]).strip("\n")
            lines = lines[:i]
            break

    title = ""
    sections: dict = {}
    body_parts: list[str] = []
    current: str | None = None      # canonical key of the OKR section being read
    current_lines: list[str] = []
    unknown_lines: list[str] = []   # accumulates non-section prose/unknown blocks

    def _flush_section():
        nonlocal current, current_lines
        if current is None:
            return
        chunk = "\n".join(current_lines).strip("\n")
        if current == "objective":
            sections["objective"] = chunk.strip()
        else:
            items = [re.sub(r"^[-*]\s+", "", ln.strip())
                     for ln in chunk.splitlines() if ln.strip()]
            sections[current] = items
        current, current_lines = None, []

    for line in lines:
        if line.startswith("# ") and not title and current is None \
                and not any(s.strip() for s in unknown_lines):
            title = line[2:].strip()
            continue
        hm = re.match(r"^##\s+(.+?)\s*$", line)
        if hm:
            canon = _SECTION_KEYS.get(hm.group(1).strip().lower())
            _flush_section()
            if canon:
                current = canon
                continue
            # unknown ## section → preserved verbatim in body
            unknown_lines.append(line)
            continue
        # Heuristic terminator (marker-less hand edits): a non-blank,
        # non-bullet line inside a LIST section means the section is over
        # and free body has begun.
        if current is not None and current != "objective" and line.strip() \
                and not line.lstrip().startswith(("-", "*")):
            _flush_section()
            unknown_lines.append(line)
            continue
        if current is not None:
            current_lines.append(line)
        else:
            unknown_lines.append(line)
    _flush_section()
    if any(s.strip() for s in unknown_lines):
        body_parts.append("\n".join(unknown_lines).strip("\n"))
    if tail:
        body_parts.append(tail)

    return {"frontmatter": fm, "title": title, "sections": sections,
            "body": "\n\n".join(body_parts)}


def update_note(path: str, **section_updates) -> dict:
    """Read-modify-write a note in place: apply the given section updates
    (``objective``/``key_results``/``facts``/``links``/``learnings``/``title``/
    ``source_url``/``body_md``), preserve everything else (unknown sections and
    free body survive via parse_note), bump ``updated_at``, and write
    atomically (tmp + rename — a concurrent reader never sees a torn note).
    Soft-error: returns ``{"ok": bool, ...}``, never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return {"ok": False, "error": f"read failed: {exc}"}
    parsed = parse_note(text)
    fm = parsed["frontmatter"]
    sec = parsed["sections"]

    # fm carries mirrored OKF+legacy keys (parse_note); accept either spelling
    # for the identity/resource/description fields on update.
    kind = str(section_updates.pop("type", None)
               or section_updates.pop("kind", None)
               or fm.get("type") or fm.get("kind") or "misc")
    key = str(section_updates.pop("key", None) or fm.get("key") or "unknown")

    def _pick(name, current):
        return section_updates.get(name, current)

    rendered = render_note(
        kind, key,
        title=_pick("title", parsed["title"] or key),
        resource=str(section_updates.get("resource",
                     _pick("source_url", fm.get("resource") or ""))),
        description=str(_pick("description", fm.get("description") or "")),
        objective=_pick("objective", sec.get("objective", "")),
        key_results=_pick("key_results", sec.get("key_results")),
        facts=_pick("facts", sec.get("facts")),
        links=_pick("links", sec.get("links")),
        learnings=_pick("learnings", sec.get("learnings")),
        body_md=_pick("body_md", parsed["body"]),
        timestamp=_now_iso(),      # the write IS the freshness event
        tags=_pick("tags", fm.get("tags")),
    )
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        os.replace(tmp, path)
    except OSError as exc:
        import contextlib
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return {"ok": False, "error": f"write failed: {exc}"}
    return {"ok": True, "path": path}


def knowledge_text(note: str) -> str:
    """The KNOWLEDGE content of a note for injection/recall — its Facts +
    consolidated free body — WITHOUT the envelope's own metadata (the
    ``## Objective`` boilerplate that describes what the file is, the title, the
    ``<!-- aiforge:body -->`` sentinel). Use this wherever a note is fed to a
    model as context (auto-injected briefs, recall ingest) so the machine
    scaffolding never reads as a project fact. A legacy/plain note with no OKR
    sections degrades to its whole body. Accepts a full note OR a
    frontmatter-stripped body (``md_store.read_file`` returns the latter)."""
    parsed = parse_note(note or "")
    sec = parsed.get("sections") or {}
    out: list[str] = []
    # measurable Key Results (a repo-note's scan counts) THEN Facts — both are
    # knowledge; a memory brief only fills Facts, a repo-note only Key Results.
    krs = sec.get("key_results") or []
    if krs:
        out.append("\n".join(f"- {k}" for k in krs))
    facts = sec.get("facts") or []
    if facts:
        out.append("\n".join(f"- {f}" for f in facts))
    # Learnings — discoveries, gotchas, dated changes — are the HIGHEST-signal
    # recall content; surface them too (they were dropped before, so a model
    # never saw a brief's gotchas). Objective boilerplate + title + sentinel
    # stay stripped (that's the scaffolding this projection exists to remove).
    learnings = sec.get("learnings") or []
    if learnings:
        # Cap: a brief's Learnings is an ever-growing audit trail; inject only
        # the most RECENT so it doesn't dominate (or evict Facts/body from) the
        # window. Full history stays on disk. Env AIFORGE_KNOWLEDGE_MAX_LEARNINGS.
        try:
            _kl = max(1, int(os.environ.get("AIFORGE_KNOWLEDGE_MAX_LEARNINGS", "12")))
        except (TypeError, ValueError):
            _kl = 12
        out.append("\n".join(f"- {l}" for l in learnings[-_kl:]))
    if parsed.get("body"):
        out.append(parsed["body"].strip())
    return "\n\n".join(out).strip() or (note or "").strip()
