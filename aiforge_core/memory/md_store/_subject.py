"""md_store internals: one note per SUBJECT, updated in place.

Captures used to be an append-only log — every mention of a thing minted a new
dated file, so the same fact survived three times at three truncation lengths.
Here a fact is folded into the note for its subject: a new claim is appended,
a re-phrasing is dropped, and a fuller version REPLACES the fragment it grew
from. Depends on `_base` (paths/parse) and `_fact` (pure comparisons).
"""
from __future__ import annotations

from pathlib import Path

from ._base import _log, _parse, _slug, captures_dir, memory_dir
from ._fact import claim_key, supersedes

_CLAIM_PREFIX = "- "


def claims_of(body: str) -> list[str]:
    """The bullet claims in a subject note's body (legacy plain bodies count as
    one claim, so a pre-existing note still folds instead of being discarded)."""
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    bullets = [ln[2:].strip() for ln in lines if ln.startswith(_CLAIM_PREFIX)]
    if bullets:
        return bullets
    joined = "\n".join(lines).strip()
    return [joined] if joined else []


def render_claims(claims: list[str]) -> str:
    return "\n".join(f"{_CLAIM_PREFIX}{c}" for c in claims)


def merge_claim(claims: list[str], claim: str) -> tuple[list[str], str]:
    """Fold ``claim`` into ``claims``. Returns ``(claims, action)`` where action
    is ``added`` / ``superseded`` / ``duplicate``."""
    new = (claim or "").strip()
    if not new:
        return claims, "duplicate"
    key = claim_key(new)
    if any(claim_key(c) == key for c in claims):
        return claims, "duplicate"
    # A fuller phrasing replaces every fragment it grew out of (the truncation
    # ladder), and a fragment of something already stored is simply dropped.
    grown_from = [c for c in claims if supersedes(new, c)]
    if grown_from:
        kept = [c for c in claims if c not in grown_from]
        return [*kept, new], "superseded"
    if any(supersedes(c, new) for c in claims):
        return claims, "duplicate"
    return [*claims, new], "added"


def _matches(d: dict, *, kind: str, repo: str, topic: str | None) -> bool:
    return (d.get("kind") == kind
            and (d.get("repo") or "") == (repo or "")
            and (d.get("topic") or "") == (topic or ""))


def find_note(subject: str, *, kind: str, repo: str,
              topic: str | None = None) -> Path | None:
    """The existing note for this subject in this scope, if any.

    Looked up by filename prefix (the stem is ``<subject-slug>-<date>-<hash>``),
    which keeps the lookup a cheap glob instead of a scan of every capture.
    """
    slug = _slug(subject)
    if not slug:
        return None
    seen: set[str] = set()
    for base in (captures_dir(), memory_dir()):
        for path in sorted(base.glob(f"{slug}-*.md")):
            if path.name in seen:
                continue
            seen.add(path.name)
            try:
                d = _parse(path)
            except Exception:  # noqa: BLE001  # an unreadable note is not a match
                continue
            if d.get("title") == subject and _matches(d, kind=kind, repo=repo,
                                                      topic=topic):
                return path
    return None


def append_claim(path: Path, claim: str, *, evidence: str | None = None) -> dict:
    """Fold one claim into an existing subject note and re-ingest it.

    Returns the parsed note plus ``action``; ``duplicate`` means the file was
    left untouched (no rewrite, no re-embed).
    """
    from ._ingest import rewrite_body

    d = _parse(path)
    claims, action = merge_claim(claims_of(d.get("body") or ""), claim)
    if action == "duplicate":
        d.pop("body", None)
        d["action"] = action
        return d
    out = rewrite_body(path, render_claims(claims), evidence=evidence)
    _log.debug("subject note %s: claim %s (%d total)", path.name, action,
               len(claims))
    out["action"] = action
    return out


__all__ = ["append_claim", "claims_of", "find_note", "merge_claim",
           "render_claims"]
