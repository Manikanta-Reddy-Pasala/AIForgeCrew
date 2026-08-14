"""Topic IDENTITY for the OKF knowledge briefs — deciding which topic a note
belongs to, deterministically wherever possible.

Why this exists
---------------
Compaction used to hand every note's title to an LLM and ask it to "cluster
these into 3-10 topics". The model saw no memory of the topics already on
disk, so every run minted fresh slugs: 142 topic briefs where ~40 subjects
exist, a third of them holding one or two facts, plus magnets like ``code`` /
``data`` / ``tmp`` / ``m`` that lexically match ANY query. Recall then injected
unrelated facts into a turn and the model blended them — the "too many md
files and it hallucinates" failure.

The fix gives the model LESS to do, not more context:

1. **Deterministic snap first** — score the note against the EXISTING topic
   briefs and take the nearest one over a cutoff. No LLM call at all for notes
   that already have a home (the common case once memory has warmed up).
2. **LLM only for the leftovers**, seeing only those leftover titles plus the
   handful of nearest existing topics — never the whole vocabulary.
3. **Admission control** — a slug must survive :func:`topic_ok` (long enough,
   not generic, carries a real word) and the lexical family snap before it may
   mint a new file.

Everything runs from ``compact()``; there is no manual cleanup step.

Similarity uses ``local_embed``, whose default ``hash`` backend is
deterministic and offline — so this path costs nothing and works with no
sidecar. Point ``AIFORGE_EMBED_BACKEND`` at a real model and the same code
becomes semantic.
"""
from __future__ import annotations

import logging
import os

from ._base import _CAPTURE_SIG_RE, _parse, brief_path, iter_briefs

log = logging.getLogger("aiforge.md_store.topics")

# Topic slugs that are RECALL MAGNETS: so generic they match almost any query,
# so a fact filed under one gets injected into unrelated turns. Anything here —
# or any slug too short to carry meaning — is refused a topic file; the note
# stays in its repo/shared brief, where scope still protects it.
GENERIC_TOPICS = {
    "api", "app", "bug", "build", "chat", "class", "cli", "code", "config",
    "core", "data", "demo", "dev", "doc", "docs", "e2e", "error", "example",
    "feature", "file", "files", "fix", "func", "function", "general", "impl",
    "info", "issue", "lib", "log", "logs", "main", "misc", "module", "note",
    "notes", "other", "output", "prod", "repo", "run", "script", "service",
    "setup", "spec", "src", "stuff", "task", "temp", "test", "tests", "tmp",
    "todo", "tool", "tools", "unit", "util", "utils", "value", "web", "work",
    # bare language/runtime names: every repo touches one, so they file nothing
    "bash", "c", "cpp", "go", "java", "js", "json", "python", "rust", "sql",
    "ts", "yaml",
}

# A topic slug must be at least this long AND carry a real word — kills the
# `m` / `mx` / `nd` / `na2` / `tw2` / `jt2` junk the eval runs minted.
MIN_TOPIC_LEN = 5


def _f_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _i_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def topic_ok(slug: str) -> bool:
    """True when ``slug`` deserves its own topic brief.

    Refuses slugs that are too short, purely generic, or carry no alphabetic
    word. A refused note falls back to its repo/shared brief rather than
    minting a magnet file that pollutes every later recall.
    """
    s = (slug or "").strip().lower()
    if len(s) < MIN_TOPIC_LEN or s in GENERIC_TOPICS:
        return False
    words = [w for w in s.split("-") if w]
    if not words:
        return False
    # every word generic ("test-data", "code-file") is still a magnet
    if all(w in GENERIC_TOPICS for w in words):
        return False
    return any(len(w) >= 3 and w.isalpha() for w in words)


def _repo_brief_names() -> set[str]:
    """Brief names that belong to the REPO axis, not the topic axis. A note must
    never snap onto one: repo briefs are per-project and folding cross-cutting
    knowledge into them (or vice versa) breaks scope, which is the one thing
    keeping another project's facts out of this turn."""
    try:
        from ..migrations import _discover_repos
        from ._base import _slug
        return {_slug(r) for r in (_discover_repos() or [])}
    except Exception:  # noqa: BLE001
        return set()


def existing_topics() -> list[str]:
    """The topic vocabulary already on disk — the slugs a labeller should REUSE
    rather than re-invent. Repo and shared briefs are NOT topics and are
    excluded; so are per-run capture files masquerading as briefs. Sorted for a
    stable (cacheable, reproducible) prompt."""
    try:
        skip = _repo_brief_names() | {"shared"}
        return sorted({p.stem[len("compacted-"):] for p in iter_briefs()
                       if p.stem[len("compacted-"):] not in skip
                       and not _CAPTURE_SIG_RE.search(p.name)})
    except Exception:  # noqa: BLE001
        return []


def semantic_ready() -> bool:
    """True only when a REAL embedding model backs ``local_embed``.

    The default ``hash`` backend is a deterministic bag-of-tokens projection:
    two briefs that share boilerplate score ~1.0 whether or not they share a
    subject. Judging topic identity with it silently fuses unrelated topics, so
    every similarity path here is gated on this and degrades to the lexical +
    LLM route instead. Override with ``AIFORGE_TOPIC_SEMANTIC=1`` to force it.
    """
    if os.environ.get("AIFORGE_TOPIC_SEMANTIC", "") in ("1", "true", "yes"):
        return True
    try:
        from .. import local_embed
        return local_embed._backend() != "hash"
    except Exception:  # noqa: BLE001
        return False


def _topic_text(slug: str, *, chars: int = 1200) -> str:
    """Representative text for an existing topic: its slug words plus the head
    of its brief. The slug alone is too thin a signal to match a note title
    against; the facts give the topic an actual subject."""
    words = slug.replace("-", " ")
    try:
        d = _parse(brief_path(slug))
        body = (d.get("body") or "")[:chars]
    except Exception:  # noqa: BLE001
        body = ""
    return f"{words}\n{body}".strip()


def _vec(text: str):
    """Embed, or ``None`` when embedding is unavailable. Never raises — a dead
    embed backend must degrade to the LLM/lexical path, not break compaction."""
    if not (text or "").strip():
        return None
    try:
        from .. import local_embed
        return local_embed.embed(text)
    except Exception as exc:  # noqa: BLE001
        log.debug("topic embed failed: %s", exc)
        return None


def _cos(a, b) -> float:
    try:
        from .. import local_embed
        return float(local_embed.cosine(a, b))
    except Exception:  # noqa: BLE001
        return 0.0


def _lexical_shortlist(text: str, topics: list[str], n: int) -> list[str]:
    """Candidate topics by word overlap with ``text`` — the no-embedder
    fallback. Enough to keep the LLM prompt small and relevant; never used to
    auto-assign, only to suggest."""
    words = {w for w in (text or "").lower().replace("-", " ").split()
             if len(w) > 2}
    if not words:
        return topics[:n]
    scored = [(len(words & set(t.split("-"))), t) for t in topics]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [t for _, t in scored[:n]]


def snap_by_similarity(text: str, topics: list[str], *,
                       cutoff: float | None = None,
                       _cache: dict | None = None) -> tuple[str | None, list[str]]:
    """Nearest EXISTING topic for ``text``, plus the ranked shortlist.

    Returns ``(hit, shortlist)`` where ``hit`` is the winning slug when its
    score clears ``cutoff`` (env ``AIFORGE_TOPIC_SNAP_CUTOFF``, default 0.62)
    and ``shortlist`` is the top candidates regardless — the caller feeds that
    small list to the LLM instead of the whole vocabulary.

    ``_cache`` lets a caller reuse one topic-vector map across many notes so a
    compaction pass embeds each topic once, not once per note.
    """
    if not topics:
        return None, []
    cut = cutoff if cutoff is not None else _f_env("AIFORGE_TOPIC_SNAP_CUTOFF", 0.62)
    n_short = _i_env("AIFORGE_TOPIC_SHORTLIST", 12)
    if not semantic_ready():
        # No real embedder: rank the shortlist lexically so the LLM still gets
        # plausible candidates, but never auto-snap on a signal that can't tell
        # subjects apart.
        return None, _lexical_shortlist(text, topics, n_short)
    qv = _vec(text)
    if qv is None:
        return None, _lexical_shortlist(text, topics, n_short)
    cache = _cache if _cache is not None else {}
    scored: list[tuple[float, str]] = []
    for t in topics:
        tv = cache.get(t)
        if tv is None:
            tv = _vec(_topic_text(t))
            if tv is None:
                continue
            cache[t] = tv
        scored.append((_cos(qv, tv), t))
    if not scored:
        return None, _lexical_shortlist(text, topics, n_short)
    scored.sort(key=lambda x: (-x[0], x[1]))
    shortlist = [t for _, t in scored[:n_short]]
    best_score, best = scored[0]
    return (best if best_score >= cut else None), shortlist


def admit(slug: str, snap) -> str | None:
    """Run a freshly-minted slug through admission control.

    ``snap`` is the lexical family snapper (``_scope._snap_topic``), injected
    to keep this module free of a circular import. Returns the canonical slug
    to use, or ``None`` when the slug must NOT become a topic file.
    """
    s = (slug or "").strip().lower().strip("-")
    if not s:
        return None
    try:
        s = snap(s)
    except Exception:  # noqa: BLE001
        pass
    # An existing topic is admitted even if its name would fail today's rules —
    # refusing it would strand the facts already filed there.
    if s in set(existing_topics()):
        return s
    return s if topic_ok(s) else None


def min_facts_for_new_topic() -> int:
    """Facts a BRAND-NEW topic must carry before it earns its own file.

    Existing topics keep receiving facts regardless; this floor only stops a
    single stray note from minting `compacted-isprime-function.md`. Env
    ``AIFORGE_TOPIC_MIN_FACTS`` (default 3, set 1 to disable)."""
    return max(1, _i_env("AIFORGE_TOPIC_MIN_FACTS", 3))


__all__ = ["GENERIC_TOPICS", "MIN_TOPIC_LEN", "topic_ok", "existing_topics",
           "snap_by_similarity", "admit", "min_facts_for_new_topic"]
