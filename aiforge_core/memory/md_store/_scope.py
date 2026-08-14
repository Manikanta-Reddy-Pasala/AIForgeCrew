"""md_store internals: the memory-SCOPE classifier (global | project | topic)
and the topic-slug snapper. Depends only on `_base`."""
from __future__ import annotations

import logging
import os

from ._base import _CAPTURE_SIG_RE, _slug, iter_briefs

log = logging.getLogger("aiforge.md_store.scope")



# ── scope classifier: global | project:<repo> | topic:<slug> ─────────────────
# A captured fact belongs to exactly one scope, which decides its brief axis:
#   global          → cross-project knowledge → compacted-shared.md
#   project:<repo>  → one repo only           → compacted-<repo>.md
#   topic:<slug>    → cross-cutting theme      → compacted-<slug>.md
# The caller's repo/topic args are HINTS; an LLM (learner role, config-driven)
# may PROMOTE a repo-hinted but universally-true fact to global — the "is this
# global or per-project?" decision the user asked compaction to understand.
_SCOPE_SYS = (
    "Classify ONE captured knowledge item into its memory SCOPE. BE "
    "CONSERVATIVE: when in doubt choose PROJECT, not global. Global is a high bar.\n"
    "- global  : TRUE FOR EVERY REPOSITORY regardless of which one — a coding or "
    "git convention, a language/tool-level lesson (Java, Python, Maven, Gradle), "
    "a machine/environment gotcha. It must NOT mention any specific repo, "
    "library, service, file, or function.\n"
    "- project : about ONE repository/service — its code, endpoints, bugs, "
    "config, or a named file/function INSIDE it (e.g. src/requests/utils.py, a "
    "Session method, a service's endpoints). Transient build/test STATUS and a "
    "task STEP are project, never global.\n"
    "- topic   : a cross-cutting theme/workflow spanning repos (e.g. data-sync, "
    "auth, ci) that is neither a single repo nor universal.\n"
    "PREFER the caller's hint. Only OVERRIDE a repo hint to global when the item "
    "is unmistakably universal (would you tell it to a developer on a completely "
    "unrelated project? if not, it is NOT global). Reply scope plus, for project "
    "the repo slug, for topic a short kebab-case topic slug."
)


def classify_scope(text: str, *, hint_repo: str | None = None,
                   hint_topic: str | None = None, role: str = "learner") -> dict:
    """Decide a captured item's memory scope → ``{scope, repo, topic}``.

    Deterministic fallback (``AIFORGE_OKR_SCOPE_LLM=0`` or the model is
    unreachable): honour the hints — repo→project, else topic→topic, else
    global — so existing capture behaviour is unchanged. With the LLM on it may
    re-route a repo-hinted fact to global when it is universally true. Never
    raises."""
    hint_repo = (hint_repo or "").strip() or None
    hint_topic = (hint_topic or "").strip() or None

    def _fallback() -> dict:
        if hint_repo:
            return {"scope": "project", "repo": hint_repo, "topic": None}
        if hint_topic:
            return {"scope": "topic", "repo": None, "topic": _slug(hint_topic)}
        # NO hints and no model verdict is not evidence of a universal truth —
        # it is absence of evidence. This branch used to return `global`, which
        # made the cheapest path award the highest-privilege scope: an eval run
        # capturing without a repo hint put `calc.py` rules in front of every
        # future turn. Unattributed knowledge is project-scoped-but-unnamed:
        # still recalled by relevance, never injected as a mandatory rule.
        return {"scope": "project", "repo": None, "topic": None}

    body = (text or "").strip()
    if not body or os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return _fallback()
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class ScopeDecision(BaseModel):
            scope: str = ""
            repo: str = ""
            topic: str = ""

        hint = f"hint_repo={hint_repo or '-'} hint_topic={hint_topic or '-'}"
        res = structured_complete(
            role,
            [{"role": "system", "content": _SCOPE_SYS},
             {"role": "user", "content": f"{hint}\n\nITEM:\n{body[:2000]}"}],
            ScopeDecision, max_tokens=200, max_retries=1, temperature=0.0)
        scope = (getattr(res, "scope", "") or "").strip().lower()
        repo = (getattr(res, "repo", "") or "").strip() or None
        topic = (getattr(res, "topic", "") or "").strip() or None
    except Exception:  # noqa: BLE001 — model down / bad JSON → honour hints
        return _fallback()

    if scope == "global":
        # Enforce the classifier's OWN rule rather than trusting it: a fact
        # naming a concrete file/path/symbol is about one codebase, whatever
        # the model said. Demote to the repo hint when we have one.
        from ..scope_guard import demote_reason
        why = demote_reason(body)
        if why:
            log.debug("scope: refusing global (%s)", why)
            return {"scope": "project", "repo": hint_repo, "topic": None}
        return {"scope": "global", "repo": None, "topic": None}
    if scope == "project":
        # An explicit PROJECT verdict is NOT global even when the model names no
        # repo (repo=None is a valid "belongs to some project" answer). Fall back
        # to hints only for the repo label, never to a different SCOPE.
        r = _slug(repo) if repo else hint_repo
        return {"scope": "project", "repo": r, "topic": None}
    if scope == "topic":
        t = topic or hint_topic
        return {"scope": "topic", "repo": None,
                "topic": _snap_topic(_slug(t)) if t else None}
    return _fallback()   # only an empty/unknown scope falls back to the hints
def _snap_topic(slug: str) -> str:
    """Snap a freshly-minted topic slug to an EXISTING topic brief when they're
    near-identical (fuzzy) — stops the classifier proliferating
    ``sync-retries`` / ``sync-retry-policy`` / ``sync-retry`` into three briefs.
    Falls back to the slug when nothing close exists. Never raises."""
    if not slug:
        return slug
    try:
        import difflib
        existing = [p.stem[len("compacted-"):]
                    for p in iter_briefs()
                    if p.stem != "compacted-shared"
                    and not _CAPTURE_SIG_RE.search(p.name)]
        if slug in existing:
            return slug
        # Prefix-containment: a slug that EXTENDS (or is extended by) an existing
        # topic at a word boundary is the same subject — 'gpsd-config' → 'gpsd',
        # 'gpsd' stays 'gpsd' even when 'gpsd-configuration' exists. Canonical =
        # the SHORTER (broader) name, so a family collapses to one brief instead
        # of gpsd / gpsd-config / gpsd-configuration. difflib's 0.82 cutoff misses
        # these (ratio 0.5-0.76). Check shortest-first for a stable target.
        for e in sorted(existing, key=len):
            if slug.startswith(e + "-") or e.startswith(slug + "-"):
                return e if len(e) <= len(slug) else slug
        m = difflib.get_close_matches(slug, existing, n=1, cutoff=0.82)
        return m[0] if m else slug
    except Exception:  # noqa: BLE001
        return slug
