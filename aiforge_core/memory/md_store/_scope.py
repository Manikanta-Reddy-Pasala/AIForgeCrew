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

# Appended when several items are classified in ONE call (see classify_scopes).
_BATCH_SYS = (
    "\n\nYou are given SEVERAL items, each on its own line as `[n] text`. "
    "Classify EACH independently and return one entry per item with its `index` "
    "= n. Judge every item on its own merit; do not let one item's scope decide "
    "another's. Return an entry for every index, in any order."
)


def _llm_scope_verdict(body: str, hint_repo, hint_topic, role: str):
    """One LLM scope classification for ``body`` → (scope, repo, topic), or None
    when the model is down / emits bad JSON (caller honours the hints)."""
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
        return ((getattr(res, "scope", "") or "").strip().lower(),
                (getattr(res, "repo", "") or "").strip() or None,
                (getattr(res, "topic", "") or "").strip() or None)
    except Exception:  # noqa: BLE001 — model down / bad JSON → honour hints
        return None


def classify_scope(text: str, *, hint_repo: str | None = None,
                   hint_topic: str | None = None, role: str = "learner") -> dict:
    """Decide a captured item's memory scope → ``{scope, repo, topic}``.

    Deterministic fallback (``AIFORGE_OKR_SCOPE_LLM=0`` or the model is
    unreachable): honour the hints — repo→project, else topic→topic, else
    (unattributed) project-but-unnamed — so existing capture behaviour is
    unchanged. With the LLM on it may re-route a repo-hinted fact to global when
    it is universally true. Never raises."""
    hint_repo = (hint_repo or "").strip() or None
    hint_topic = (hint_topic or "").strip() or None

    def _fallback() -> dict:
        if hint_repo:
            return {"scope": "project", "repo": hint_repo, "topic": None}
        if hint_topic:
            return {"scope": "topic", "repo": None, "topic": _slug(hint_topic)}
        # NO hints and no model verdict is not evidence of a universal truth — it
        # is absence of evidence. This used to return `global`, making the
        # cheapest path award the highest-privilege scope (an eval run without a
        # repo hint put `calc.py` rules in front of every future turn).
        # Unattributed knowledge is project-scoped-but-unnamed: recalled by
        # relevance, never injected as a mandatory rule.
        return {"scope": "project", "repo": None, "topic": None}

    body = (text or "").strip()
    if not body or os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return _fallback()
    verdict = _llm_scope_verdict(body, hint_repo, hint_topic, role)
    if verdict is None:
        return _fallback()
    scope, repo, topic = verdict
    return _verdict(scope, repo, topic, body, hint_repo, hint_topic, _fallback)


def _verdict(scope, repo, topic, body, hint_repo, hint_topic, fallback) -> dict:
    """One model verdict → the scope dict, with the guards applied. Shared by
    the single-item and the batched classifier so they cannot drift."""
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
    return fallback()   # only an empty/unknown scope falls back to the hints


# How many items go in ONE batched classification, and the char budget for
# their combined body. One call per item re-sent the ~1.4k-char rule prompt
# every time: a single chat day (10 windows x 8 items) cost 80 calls and 89k
# chars of prompt, ~90% of the whole fold. Batching makes that 10 calls.
_BATCH_MAX = 20
_BATCH_CHARS = 8000
# Same per-item clip as the single-item classifier (body[:2000]) so batching
# cannot change a verdict just by showing the model less of the item.
_BATCH_ITEM_CHARS = 2000


def classify_scopes(texts: "list[str]", *, hint_repo: str | None = None,
                    hint_topic: str | None = None,
                    role: str = "learner") -> "list[dict]":
    """Scope MANY items with one call per batch → a dict per input, in order.

    Same rules, same guards, same fallbacks as :func:`classify_scope` — only
    the transport differs. Any batch the model fails on falls back per item to
    the hints (never to a single-item call storm)."""
    items = [(t or "").strip() for t in (texts or [])]
    if not items:
        return []
    if os.environ.get("AIFORGE_OKR_SCOPE_LLM", "1") == "0":
        return [classify_scope(t, hint_repo=hint_repo, hint_topic=hint_topic,
                               role=role) for t in items]     # deterministic
    out: "list[dict | None]" = [None] * len(items)
    batch: list = []
    used = 0
    for idx, body in enumerate(items):
        if not body:
            out[idx] = classify_scope("", hint_repo=hint_repo,
                                      hint_topic=hint_topic, role=role)
            continue
        # ONE LINE PER ITEM: the listing is `[n] text`, so an embedded newline
        # would look like the start of an unnumbered item and slide the model's
        # indices — the wrong item's scope, silently.
        clipped = " ⏎ ".join(body[:_BATCH_ITEM_CHARS].splitlines())
        if batch and (len(batch) >= _BATCH_MAX or used + len(clipped) > _BATCH_CHARS):
            _run_batch(batch, out, items, hint_repo, hint_topic, role)
            batch, used = [], 0
        batch.append((idx, clipped))
        used += len(clipped)
    if batch:
        _run_batch(batch, out, items, hint_repo, hint_topic, role)
    # Every slot is filled by _run_batch; this is belt-and-braces for a future
    # early return — and it uses the SAME fallback as everything else, so the
    # three paths cannot disagree about what a hintless item scopes to.
    return [o if o is not None
            else dict(_hint_scope(hint_repo, hint_topic), fallback=True)
            for o in out]


def _classify_batch(batch, hint_repo, hint_topic, role) -> list:
    """One LLM call classifying ``batch`` items' scope. Returns the model's list
    of scope entries, or [] when the model is down / emits bad JSON (→ hints)."""
    try:
        from pydantic import BaseModel
        from aiforge_core.llm.structured import structured_complete

        class ScopeItem(BaseModel):
            index: int = -1
            scope: str = ""
            repo: str = ""
            topic: str = ""

        class ScopeDecisions(BaseModel):
            # Unquoted on purpose: the string form hid the only reference to
            # ScopeItem, so it read as a class nobody used. `from __future__
            # import annotations` turns this back into the same string before
            # pydantic ever sees it — identical at runtime, honest to a reader.
            items: list[ScopeItem] = []

        hint = f"hint_repo={hint_repo or '-'} hint_topic={hint_topic or '-'}"
        listing = "\n".join(f"[{n}] {body}" for n, (_, body) in enumerate(batch))
        res = structured_complete(
            role,
            [{"role": "system", "content": _SCOPE_SYS + _BATCH_SYS},
             {"role": "user", "content": f"{hint}\n\nITEMS:\n{listing}"}],
            ScopeDecisions, max_tokens=60 * len(batch) + 120, max_retries=1,
            temperature=0.0)
        return list(getattr(res, "items", None) or [])
    except Exception as exc:  # noqa: BLE001 — model down / bad JSON → hints
        log.debug("batched scope classification failed: %s", exc)
        return []


def _verdicts_by_index(entries, batch_len: int) -> dict:
    """Map batch-index → the FIRST valid scope entry for it. A repeated or
    out-of-range index is dropped rather than overwriting a good answer."""
    verdicts: dict = {}
    for it in entries:
        try:
            n = int(getattr(it, "index", -1))
        except Exception:  # noqa: BLE001
            continue
        if 0 <= n < batch_len and n not in verdicts:
            verdicts[n] = it
    return verdicts


def _apply_batch_verdict(it, body, out, idx, hint_repo, hint_topic) -> None:
    """Write one item's scope verdict into ``out``. Honours the hints (flagged
    ``fallback=True``) when the model gave no verdict or a guard blew up — one
    failed batch used to read as N confident "project" verdicts, and
    cleanup_reheal DELETES what isn't global."""
    if it is None:
        out[idx] = dict(_hint_scope(hint_repo, hint_topic), fallback=True)
        return
    try:
        out[idx] = _verdict((getattr(it, "scope", "") or "").strip().lower(),
                            (getattr(it, "repo", "") or "").strip() or None,
                            (getattr(it, "topic", "") or "").strip() or None,
                            body, hint_repo, hint_topic,
                            lambda: _hint_scope(hint_repo, hint_topic))
    except Exception as exc:  # noqa: BLE001 — a guard blew up on ONE item
        log.debug("scope verdict failed: %s", exc)
        out[idx] = dict(_hint_scope(hint_repo, hint_topic), fallback=True)


def _run_batch(batch, out, items, hint_repo, hint_topic, role) -> None:
    """One LLM call for ``batch`` = [(index, clipped body)]. Never raises."""
    entries = _classify_batch(batch, hint_repo, hint_topic, role)
    verdicts = _verdicts_by_index(entries, len(batch))
    for n, (idx, _clipped) in enumerate(batch):
        _apply_batch_verdict(verdicts.get(n), items[idx], out, idx,
                             hint_repo, hint_topic)


def _hint_scope(hint_repo, hint_topic) -> dict:
    if hint_repo:
        return {"scope": "project", "repo": hint_repo, "topic": None}
    if hint_topic:
        return {"scope": "topic", "repo": None, "topic": _slug(hint_topic)}
    return {"scope": "project", "repo": None, "topic": None}
def _existing_topic_slugs() -> list[str]:
    """The topic slugs of the existing (non-shared, non-capture) briefs."""
    return [p.stem[len("compacted-"):]
            for p in iter_briefs()
            if p.stem != "compacted-shared" and not _CAPTURE_SIG_RE.search(p.name)]


def _prefix_family_match(slug: str, existing: list[str]) -> "str | None":
    """Snap ``slug`` to an existing topic it EXTENDS or is extended by at a word
    boundary — the same subject ('gpsd-config' → 'gpsd'). Canonical = the SHORTER
    (broader) name, so a family collapses to one brief. Shortest-first for a
    stable target. difflib's 0.82 cutoff misses these (ratio 0.5-0.76)."""
    for e in sorted(existing, key=len):
        if slug.startswith(e + "-") or e.startswith(slug + "-"):
            return e if len(e) <= len(slug) else slug
    return None


def _snap_topic(slug: str) -> str:
    """Snap a freshly-minted topic slug to an EXISTING topic brief when they're
    near-identical (fuzzy) — stops the classifier proliferating
    ``sync-retries`` / ``sync-retry-policy`` / ``sync-retry`` into three briefs.
    Falls back to the slug when nothing close exists. Never raises."""
    if not slug:
        return slug
    try:
        import difflib
        existing = _existing_topic_slugs()
        if slug in existing:
            return slug
        family = _prefix_family_match(slug, existing)
        if family is not None:
            return family
        m = difflib.get_close_matches(slug, existing, n=1, cutoff=0.82)
        return m[0] if m else slug
    except Exception:  # noqa: BLE001
        return slug
