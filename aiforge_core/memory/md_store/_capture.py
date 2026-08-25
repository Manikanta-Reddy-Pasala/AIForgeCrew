"""md_store internals: the unified `capture()` entry every learning flows
through, plus its accepted `kind` set. Builds on `_base`, `_scope`, `_ingest`
and `_render`."""
from __future__ import annotations

from ._base import _slug
from ._ingest import write
from ._render import _brief_upsert
from ._scope import classify_scope



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
def _promote_scope(text: str, repo: "str | None", topic: "str | None",
                   classify: bool) -> "tuple[str | None, str | None]":
    """(repo, topic) after a promotion check: a repo-hinted fact that is actually
    cross-project is PROMOTED to the shared (global) brief (repo/topic → None).
    Promotion-only — never demote a global capture into a repo — so the
    deterministic/off path leaves existing behaviour untouched. ``classify=False``
    (a caller that already resolved scope) skips the LLM call."""
    if not (repo and classify):
        return repo, topic
    try:
        if classify_scope(text, hint_repo=repo, hint_topic=topic)["scope"] == "global":
            return None, None
    except Exception:  # noqa: BLE001 — scope upkeep never breaks a write
        pass
    return repo, topic


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
    repo, topic = _promote_scope(text, repo, topic, classify)
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
    # just-written data instead of waiting for the periodic compaction. Global
    # writes (no repo) maintain the SHARED brief (compacted-shared.md), which
    # _project_brief unions into every context.
    try:
        _brief_upsert(repo or "shared", text, topic=topic)
    except Exception:  # noqa: BLE001 — brief upkeep never breaks a write
        pass
    return res