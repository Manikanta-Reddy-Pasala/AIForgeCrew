"""md_store internals: the unified `capture()` entry every learning flows
through, plus its accepted `kind` set. Builds on `_base`, `_scope`, `_ingest`,
`_fact` (is this a fact, and about what) and `_subject` (one note per subject).
"""
from __future__ import annotations

from . import _fact, _subject
from ._base import _log, _slug
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
    # constraint — a standing rule. Its own kind so the global-rescope self-heal
    # (which demotes any GLOBAL learning naming a file/path out of global scope)
    # cannot silently unscope "never hand-edit ~/.aiforge/security": that pass
    # only looks at type=="learning".
    "constraint",
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
    except Exception:  # noqa: BLE001  # scope upkeep never breaks a write
        pass
    return repo, topic


def _reject(text: str, reasons: list[str], kind: str, source: str) -> dict:
    """A string that is not a fact is DROPPED, loudly enough to audit.

    Silently accepting everything is what filled memory with CLI fragments,
    headings and raw chat turns; each one then cost an embed, a brief slot and
    a line of recall context forever.
    """
    _log.info("capture: dropped %s from %s (%s): %r",
              kind, source, "; ".join(reasons), text[:80])
    return {"skipped": "not_a_fact", "reasons": reasons}


def capture(kind: str, text: str, *, repo: str | None = None,
            topic: str | None = None, title: str | None = None,
            source: str = "capture", tags: list[str] | None = None,
            ingest: bool = True, classify: bool = True,
            subject: str | None = None, evidence: str | None = None,
            confidence: str | None = None) -> dict:
    """Persist one captured FACT as an md memory (repo + topic stamped + tagged),
    so it flows into both compaction axes.

    The text must read as a durable claim (see :mod:`_fact`); a fragment,
    heading, question or raw chat turn is dropped with ``{"skipped":
    "not_a_fact"}``. Facts are keyed by ``subject`` (derived when not given):
    a new claim about a known subject is folded into that subject's note rather
    than minting another dated file. ``kind`` should be one of
    ``_CAPTURE_KINDS`` (falls back to a plain note otherwise).
    """
    text = (text or "").strip()
    if not text:
        return {"skipped": "empty"}
    ok, reasons = _fact.is_wellformed(text)
    if not ok:
        return _reject(text, reasons, kind, source)
    k = kind if kind in _CAPTURE_KINDS else "note"
    repo, topic = _promote_scope(text, repo, topic, classify)
    subj = (subject or "").strip() or _fact.derive_subject(text)
    ttl = title or _fact.title_for(subj, text)
    # Only a subject the caller STATED, or one the text names outright, is
    # trustworthy enough to merge two captures into one note — see
    # _fact.strong_subject.
    may_fold = bool((subject or "").strip()) or bool(_fact.strong_subject(text))
    res = _write_or_fold(ttl, text, kind=k, repo=repo, topic=topic, subject=subj,
                         source=source, tags=tags, ingest=ingest,
                         evidence=evidence, confidence=confidence,
                         may_fold=may_fold)
    # WRITE-TIME brief maintenance: fold the fact into the repo's compacted brief
    # RIGHT NOW (cheap, no LLM), so recall (which reads compacted-<repo>.md) sees
    # just-written data instead of waiting for the periodic compaction. Global
    # writes (no repo) maintain the SHARED brief (compacted-shared.md), which
    # _project_brief unions into every context.
    try:
        _brief_upsert(repo or "shared", text, topic=topic)
    except Exception:  # noqa: BLE001  # brief upkeep never breaks a write
        pass
    return res


def _write_or_fold(ttl: str, text: str, *, kind: str, repo: str | None,
                   topic: str | None, subject: str, source: str,
                   tags: list[str] | None, ingest: bool,
                   evidence: str | None, confidence: str | None,
                   may_fold: bool = True) -> dict:
    """Fold the claim into this subject's existing note, else write a new one."""
    existing = None
    try:
        if may_fold:
            existing = _subject.find_note(ttl, kind=kind, repo=repo or "shared",
                                          topic=topic)
    except Exception:  # noqa: BLE001  # a lookup failure must not lose the fact
        existing = None
    if existing is not None:
        return _subject.append_claim(existing, text, evidence=evidence)
    tset = list(tags or [])
    if repo:
        tset.append(f"repo:{_slug(repo)}")
    if topic:
        tset.append(f"topic:{_slug(topic)}")
    tset.append(kind)
    res = write(ttl, text, kind=kind, tags=list(dict.fromkeys(tset)),
                source=source, repo=repo or "shared", topic=topic, ingest=ingest,
                subject=subject, evidence=evidence, confidence=confidence)
    res["action"] = "created"
    return res
