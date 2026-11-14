"""Re-page split topics: one topic, one clean set of parts.

A topic that outgrows the split cap is written as ``compacted-<t>.md`` +
``compacted-<t>-2.md`` … with "Part i of n" / "Continued in" lines that link
them. On live memory that bookkeeping had rotted: the link lines were kept as
FACTS (part 1 of ``shared`` said "Part 2 of 3"), a part was split again
(``shared-2-2``, "Part 2 of 2 · main topic: shared-2"), a link pointed at an
unrelated topic, and a part survived its main topic. In the Memory page that is
the same topic name several times, at different lengths.

For every topic that has parts: gather the facts of the head and of every part
(nested ones too), drop the link lines, de-duplicate, and render the topic
again through the normal pager (``_brief_parts``) — so the parts are re-cut
with correct links — then archive the files the new set no longer has. A file
counts as a part only when its head exists or its own text says it is one
(a real slug can end in a number: ``log4j-2``). An orphan part (its head gone)
becomes the head. Deterministic, no model. Off: ``AIFORGE_COMPACT_REPAGE=0``.
"""
from __future__ import annotations

import logging
import os
import re

_log = logging.getLogger("aiforge.memory.repage")
_PART_SUFFIX = re.compile(r"(?:-\d+)+$")
_PART_MARK = re.compile(r"\(part \d+/\d+\)|Part \d+ of \d+", re.I)
#: link lines the pager writes; never facts
_XREF = re.compile(r"^\W*(?:part \d+ of \d+\b|continued in\b|main topic\b)", re.I)


def _on() -> bool:
    return os.environ.get("AIFORGE_COMPACT_REPAGE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _clean(items: list) -> list:
    out, seen = [], set()
    for it in items or []:
        s = str(it).strip()
        if not s or _XREF.search(s):
            continue
        k = re.sub(r"\W+", " ", s.lower()).strip()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _groups(paths) -> dict:
    """``{head_slug: [part paths…]}`` for heads with parts, and orphan parts
    keyed by the head they name. Only files the pager made count as parts."""
    stems = {p.stem[len("compacted-"):]: p for p in paths}
    groups: dict[str, list] = {}
    for slug, p in stems.items():
        if not _PART_SUFFIX.search(slug):
            continue
        head = _PART_SUFFIX.sub("", slug)
        if not head:
            continue
        if head in stems:
            groups.setdefault(head, []).append(p)
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if _PART_MARK.search(text):
            groups.setdefault(head, []).append(p)          # orphan part
    return groups


def repage_split_topics() -> dict:
    """Re-cut every split topic's parts (see module docstring)."""
    if not _on():
        return {"repaged": 0, "skipped": "disabled"}
    from aiforge_core.config import _atomic
    from .._base import brief_path, briefs_dir, iter_briefs
    from .._compact_parts import _brief_parts
    from .._render import _parse_brief
    from ._reconcile_merge import _drop_brief

    done, retired = [], 0
    for head, parts in sorted(_groups(iter_briefs()).items()):
        try:
            hpath = brief_path(head)
            files = ([hpath] if hpath.exists() else []) + sorted(
                parts, key=lambda p: (len(p.stem), p.stem))
            acc = {"facts": [], "learnings": [], "links": [], "key_results": [],
                   "sources": [], "tags": [], "title": "", "body": ""}
            for f in files:
                b = _parse_brief(f.read_text(encoding="utf-8", errors="replace"))
                for k in ("facts", "learnings", "links", "key_results", "sources", "tags"):
                    acc[k].extend(b.get(k) or [])
                if f == files[0]:
                    acc["title"] = re.sub(r"\s*\(part \d+/\d+\)\s*", " ",
                                          b.get("title") or "").strip()
                    acc["body"] = "\n".join(ln for ln in (b.get("body") or "").splitlines()
                                            if not _XREF.search(ln) and not ln.startswith("#"))
            sections = {k: _clean(acc[k]) for k in ("facts", "learnings", "links", "key_results")}
            new_parts = _brief_parts(head, sections, sorted(set(acc["tags"])),
                                     acc["title"] or head,
                                     sources=sorted(set(acc["sources"])) or None,
                                     body_md=acc["body"].strip())
            for stem, content in new_parts:
                _atomic.write_text(str(briefs_dir() / f"{stem}.md"), content)
            keep = {stem for stem, _ in new_parts}
            for f in files:
                if f.stem not in keep:
                    _drop_brief(f, f.stem[len("compacted-"):], [])
                    retired += 1
            done.append(head)
        except Exception as exc:  # noqa: BLE001 — one bad topic never stops the rest
            _log.warning("repage %s skipped: %s", head, exc)
    if done:
        _log.info("repage: %d topic(s) re-cut, %d stale part file(s) archived: %s",
                  len(done), retired, ", ".join(done[:20]))
    return {"repaged": len(done), "retired": retired, "topics": done}
