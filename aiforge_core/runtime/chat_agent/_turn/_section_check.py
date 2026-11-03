"""A spec section is read once more before it closes.

The board of a sectioned spec has one item per section ("## 3. Partial
returns"). A benchmark run closed every section with its own tests green and
the spelled-out names present — 26/60 on the hidden tests: "partial returns"
became a restock on cancel, most numbered requirements had no test (29 new
tests where OpenCode wrote ~86). The first time the model marks such an item
done, the harness hands it that section's own text and asks for a check of
each numbered requirement, a test for each that has none; the second "done"
stands. Once per section; nothing is asked of a model.
Off switch: ``AIFORGE_CHAT_SECTION_CHECK=0``.
"""
from __future__ import annotations

import os
import re

_SECTION_CHARS = 6000


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_SECTION_CHECK", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _body_for(goal: str, slug: str, title: str) -> str:
    from ._plan_llm import section_texts
    secs = section_texts(goal)
    if not secs:
        return ""
    m = re.match(r"^part-(\d+)$", slug or "")
    if m and 1 <= int(m.group(1)) <= len(secs):
        return secs[int(m.group(1)) - 1][1]
    t = " ".join(str(title or "").lower().split())
    for head, body in secs:
        if " ".join(head.lower().split()) in t:
            return body
    return ""


def check_back(st, result: dict, events: list, before: dict):
    """``(result, events)``: unchanged, or — the first close of a section
    item — the close put back with the section's text to check against."""
    try:
        slug = result.get("slug") if isinstance(result, dict) else None
        item = (getattr(st, "board", None) or {}).get(slug)
        if (not _on() or not result.get("ok") or item is None
                or item.get("status") != "done" or before.get(slug) == "done"
                or item.get("section_checked") or not item.get("from_request")):
            return result, events
        body = _body_for(str(getattr(st, "goal", "") or ""), slug, item.get("title"))
        if not body:
            return result, events
        item["section_checked"] = True
        item["status"] = before.get(slug) if before.get(slug) not in (None, "done") else "running"
        cut = body[:_SECTION_CHARS] + ("\n…" if len(body) > _SECTION_CHARS else "")
        res = {"ok": False, "slug": slug, "status": item["status"],
               "error": "not closed yet: check it against the request first",
               "check": ("Before this section is done, go through the request's own text "
                         "for it, below, requirement by requirement: is each one "
                         "implemented exactly as written (names, parameters, errors, "
                         "edge cases), and does a test of yours cover it? Fix and add "
                         "what is missing, run the tests, then mark it done again."),
               "section": cut}
        return res, [{"type": "subtask_update", "slug": slug, "status": item["status"]}]
    except Exception:  # noqa: BLE001 — the check never breaks a step
        return result, events


__all__ = ["check_back"]
