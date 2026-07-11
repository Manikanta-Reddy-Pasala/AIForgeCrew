"""Open Knowledge Format (OKF v0.1) — the canonical rules the memory system
writes to, and the LLM-facing block injected into the compaction + learner
prompts so both PRODUCE OKF-compliant bundles.

OKF (Google Cloud, v0.1) makes a directory of Markdown files a portable
knowledge graph: each file is one concept (YAML frontmatter + Markdown body),
the file PATH is its identity, and Markdown links between files are the edges.
This module is the single source of truth — do not restate these rules inline
elsewhere; import ``OKF_RULES`` (LLM prompt block) / ``okf_frontmatter`` /
``append_log`` / ``render_index`` instead.

See ``docs/OKF.md`` for the human reference.
"""
from __future__ import annotations

import os
import re

# ── The producer rules, as an LLM prompt block ───────────────────────────
# Injected verbatim into every memory-writing LLM step (compaction consolidate,
# learner fact distillation, OKR authoring) so the model emits OKF-valid files.
OKF_RULES = (
    "OPEN KNOWLEDGE FORMAT (OKF v0.1) — every knowledge/memory file you write "
    "MUST follow this so the notes stay a portable, linkable knowledge graph:\n"
    "\n"
    "STRUCTURE (hard rules):\n"
    "- One concept = ONE UTF-8 Markdown file: a YAML frontmatter block delimited "
    "by `---` on its own line at the very TOP, then a free-form Markdown body.\n"
    "- Frontmatter MUST contain a non-empty `type:` field (e.g. type: objective, "
    "type: key_result, type: learning, type: session, type: runbook, type: table).\n"
    "\n"
    "IDENTITY & LINKS:\n"
    "- The FILE PATH is the concept's identity (e.g. /learnings/L-03.md). No "
    "external IDs or databases.\n"
    "- Link concepts with Markdown links to build the graph. PREFER absolute "
    "bundle-relative links starting with `/` — e.g. [the rule](/objectives/O-01.md). "
    "Relative links (./other.md) are also allowed.\n"
    "- Edges are UNTYPED: state the MEANING of a link (parent-of, depends-on, "
    "supersedes) in the PROSE around it, never in the link syntax.\n"
    "\n"
    "RECOMMENDED frontmatter fields — add each when known, in this priority order:\n"
    "1. title — human-readable display name.\n"
    "2. description — ONE sentence summarizing the concept (used as a search snippet).\n"
    "3. resource — a URI identifying an underlying asset (a DB table, a repo path), if any.\n"
    "4. tags — a YAML list of short strings for categorization.\n"
    "5. timestamp — ISO-8601 datetime of the last meaningful change.\n"
    "\n"
    "RESERVED FILES (only if you write them):\n"
    "- index.md — navigation ONLY; MUST have NO frontmatter; lists contents to help "
    "humans/agents navigate.\n"
    "- log.md — audit trail; a flat list of ISO-8601 date headings (`## 2026-07-11`), "
    "newest first.\n"
    "\n"
    "CONSUMER RULES (when you READ a bundle — be forgiving, never reject):\n"
    "- Tolerate unknown `type` values and any unknown/custom frontmatter keys "
    "(preserve them verbatim).\n"
    "- Tolerate broken cross-links — a broken link is knowledge not yet written, "
    "NOT an error.\n"
    "- Never reject a file for missing optional fields, nor a bundle for a missing "
    "index.md / log.md.\n"
)

# Recommended optional fields, priority order (spec §5).
RECOMMENDED_FIELDS = ("title", "description", "resource", "tags", "timestamp")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def okf_frontmatter(type_: str, *, title: str = "", description: str = "",
                    resource: str = "", tags: "list[str] | None" = None,
                    timestamp: str = "", extra: "dict | None" = None) -> str:
    """Render an OKF-valid YAML frontmatter block. ``type`` is required (hard
    rule); the recommended fields are emitted in spec priority order when
    non-empty; ``extra`` carries any custom keys (edges etc.) — consumers must
    preserve them, so we keep them. Returns the `---`-delimited block only."""
    lines = ["---", f"type: {(type_ or 'note').strip()}"]
    if title:
        lines.append(f"title: {_scalar(title)}")
    if description:
        lines.append(f"description: {_scalar(description)}")
    if resource:
        lines.append(f"resource: {_scalar(resource)}")
    if tags:
        clean = [str(t).strip() for t in tags if str(t).strip()]
        if clean:
            lines.append("tags: [" + ", ".join(_scalar(t) for t in clean) + "]")
    if timestamp:
        lines.append(f"timestamp: {timestamp}")
    for k, v in (extra or {}).items():
        if v in (None, "", [], {}):
            continue
        lines.append(f"{k}: {_render_value(v)}")
    lines.append("---")
    return "\n".join(lines)


def append_log(log_path: str, entry: str, *, date: str) -> None:
    """Append ``entry`` under the ISO-8601 ``date`` heading in a reserved
    log.md, keeping headings NEWEST-FIRST (spec §3). ``date`` must be ISO-8601
    (YYYY-MM-DD) — callers pass it in (no clock here so this stays testable /
    reproducible). Creates the file if absent. Idempotent-ish: appends a bullet
    under the day's heading, inserting the heading at the top if it's new."""
    date = (date or "").strip()
    if not date:
        return
    bullet = f"- {entry.strip()}"
    existing = ""
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8") as fh:
            existing = fh.read()
    heading = f"## {date}"
    if heading in existing:
        # insert the bullet right after the existing heading line
        out = existing.replace(heading + "\n", heading + "\n" + bullet + "\n", 1)
    else:
        # new day → prepend (newest first), after an optional title line
        block = f"{heading}\n{bullet}\n\n"
        if existing.startswith("# "):
            head, _, rest = existing.partition("\n")
            out = f"{head}\n\n{block}{rest.lstrip()}"
        else:
            out = block + existing
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(out)
    _rotate_log(log_path, out)


def _rotate_log(log_path: str, text: str) -> None:
    """Split an oversized log.md so no single file grows unbounded: keep the
    newest date sections in log.md, move the older half to a sibling
    ``log-archive.md`` (newest-first, appended). Cap via AIFORGE_OKF_LOG_MAX_BYTES
    (default 64 KB). No-op below the cap."""
    try:
        cap = int(os.environ.get("AIFORGE_OKF_LOG_MAX_BYTES", "65536"))
    except (TypeError, ValueError):
        cap = 65536
    if cap <= 0 or len(text.encode("utf-8")) <= cap:
        return
    # split on date headings (## YYYY-MM-DD) — keep the newest ~half, archive rest
    parts = re.split(r"(?m)(?=^## \d{4}-\d{2}-\d{2}\b)", text)
    head = [p for p in parts if not p.strip().startswith("## ")]
    days = [p for p in parts if p.strip().startswith("## ")]
    if len(days) < 4:
        return
    keep_n = max(1, len(days) // 2)
    keep, archive = days[:keep_n], days[keep_n:]
    try:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("".join(head) + "".join(keep))
        arch = os.path.join(os.path.dirname(log_path), "log-archive.md")
        prev = ""
        if os.path.isfile(arch):
            with open(arch, encoding="utf-8") as fh:
                prev = fh.read()
        with open(arch, "w", encoding="utf-8") as fh:
            fh.write("".join(archive) + prev)
    except Exception:  # noqa: BLE001 — rotation is best-effort
        pass


def render_index(title: str, entries: "list[tuple[str, str]]") -> str:
    """Render a reserved index.md — navigation ONLY, NO frontmatter (spec §3).
    ``entries`` = [(absolute_bundle_path, one-line hook), …]."""
    lines = [f"# {title}", ""]
    for path, hook in entries:
        link = path if path.startswith("/") else "/" + path.lstrip("./")
        lines.append(f"- [{path.rsplit('/', 1)[-1]}]({link})"
                     + (f" — {hook}" if hook else ""))
    return "\n".join(lines) + "\n"


def validate_file(text: str, *, is_reserved: bool = False) -> list[str]:
    """Return OKF conformance violations for one file's text (empty = valid).
    Reserved files (index.md/log.md) are exempt from the frontmatter rule."""
    errs: list[str] = []
    if is_reserved:
        return errs
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return ["missing YAML frontmatter block at the top (--- … ---)"]
    fm = m.group(1)
    if not re.search(r"(?m)^type:\s*\S+", fm):
        errs.append("frontmatter has no non-empty `type:` field")
    return errs


def _scalar(s: str) -> str:
    s = str(s).replace("\n", " ").strip()
    if s and (s[0] in "[{>|@`\"'#&*!%" or ":" in s or s.endswith(":")):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _render_value(v) -> str:
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_scalar(str(x)) for x in v) + "]"
    if isinstance(v, dict):
        import json
        return json.dumps(v, ensure_ascii=False)
    return _scalar(str(v))


__all__ = ["OKF_RULES", "RECOMMENDED_FIELDS", "okf_frontmatter",
           "append_log", "render_index", "validate_file"]
