"""Researcher skip-routing decision.

Researcher is a read-only context gatherer that fires once per ticket
between the Verifier and the Doer LoopAgent (see :mod:`pipeline`). It
costs 5+ LM calls upfront — graphify_lookup, memory_lookup, file_read
across every subticket — which is a waste for greenfield work where
nothing the Researcher could surface exists yet.

ONE-117 was the canary: a brand-new FastAPI scaffold ticket spent ~4
minutes inside the Researcher with zero relevant_files because the
target repo had no prior commits matching the ticket's keywords. The
Doer would have produced the same output without the brief.

Skip rule (BOTH conditions must hold):
  1. Ticket body has NO patterns suggesting prior context exists —
     no ``refer``/``see``/``like``/``previously``/``existing`` words.
  2. ``AIFORGE_REPO_ROOT`` git log has 0 commits whose subject
     contains the ticket's primary project keyword.

Override knob: ``AIFORGE_RESEARCHER_FORCE=1`` runs the Researcher
unconditionally (debugging / known-mixed greenfield-plus-brownfield).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess


log = logging.getLogger("aiforge.researcher_routing")


# Patterns whose presence in the body signals "this ticket references
# prior work" — anchored with regex so substrings ("reference" must
# match "refer to" but NOT "deference"; "see " must not trip on
# "seed"). We keep them conservative so we don't accidentally skip
# Researcher on a brownfield ticket.
_REFERENCE_PATTERNS: tuple[str, ...] = (
    r"\brefer\b",            # "refer to ...", but not "deference"
    r"\brefer\s+to\b",       # explicit "refer to" phrase
    r"\bsee\b",              # "see foo.py"
    r"\blike\s+(the|in|we)", # "like the existing", "like in foo"
    r"\bpreviously\b",
    r"\bexisting\b",
    r"\bprior\b",
    r"\bearlier\b",
    r"above[- ]mentioned",
    r"\bas\s+in\b",
    r"\bsimilar\s+to\b",
)


# Words we strip when picking project keywords from the ticket title —
# generic verbs / nouns that match too many commits to be useful as
# a "has the project ever been touched" probe. We probe ALL surviving
# tokens against git log; ANY match → don't skip.
_KEYWORD_STOPWORDS: frozenset[str] = frozenset({
    "add", "fix", "update", "remove", "delete", "create", "build",
    "make", "implement", "refactor", "improve", "the", "a", "an",
    "to", "for", "of", "in", "on", "and", "or", "with", "from",
    "ticket", "feature", "bug", "service", "scaffold", "stack",
    "bootstrap", "setup", "configure", "support",
})


def _has_reference_pattern(body: str) -> bool:
    """``True`` when the body mentions prior work the Researcher would
    actually find. Cheap pre-compiled regex — runs once per ticket.

    Patterns are anchored with ``\b`` so substrings like ``deference``
    or ``seed`` don't false-positive on ``refer`` / ``see``.
    """
    if not body:
        return False
    text = body.lower()
    for pattern in _REFERENCE_PATTERNS:
        if re.search(pattern, text):
            return True
    return False


def _project_keywords(title: str) -> list[str]:
    """Return the set of non-stopword tokens in ``title`` that are
    worth probing the git log for. Empty list when nothing survives
    filtering — caller falls back to "skip if reference patterns
    are also absent".

    We probe ALL surviving tokens (not just the first) because the
    "project name" can appear anywhere in the title and is rarely the
    leading verb. Example: ``"Bootstrap zoozle service"`` → ``["zoozle"]``
    once stopwords (``bootstrap``, ``service``) are stripped.
    """
    if not title:
        return []
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", title)
    return [t for t in tokens if t.lower() not in _KEYWORD_STOPWORDS]


def _git_log_has_keyword(repo_root: str, keyword: str) -> bool:
    """``True`` when any commit subject in the repo mentions
    ``keyword`` (case-insensitive). Anything more than 0 commits is
    enough signal that the Researcher will find SOMETHING worth
    surfacing."""
    if not repo_root or not os.path.isdir(os.path.join(repo_root, ".git")):
        # No repo to probe — be conservative and DON'T skip; let
        # Researcher run and decide for itself.
        return True
    try:
        # ``--grep=<pat>`` is case-INsensitive only with ``-i``. We
        # cap output to one line because we only need a yes/no.
        proc = subprocess.run(
            ["git", "log", "-i", "--grep", keyword,
             "--oneline", "-1"],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("researcher_routing.git_probe_failed: %s", exc)
        return True   # be conservative on probe failure
    return bool((proc.stdout or "").strip())


def should_skip_researcher(
    title: str,
    body: str,
    repo_root: str | None = None,
) -> tuple[bool, str]:
    """Return ``(skip, reason)``. ``skip=True`` when BOTH conditions
    hold AND the override env var is unset.

    ``reason`` is a short tag for trace logging (``forced_on``,
    ``has_reference_word``, ``git_log_match``, ``greenfield``).
    """
    # Explicit skip overrides everything. Useful when the operator
    # has hand-curated the ticket body with file paths + line numbers
    # so the Researcher's web/code lookup adds no value AND the
    # model sometimes returns empty (observed on
    # ticket bodies past ~8KB). Without this knob, pipelines on
    # hand-curated brownfield tickets would die at the Researcher
    # stage with EscalatingLlm-exhausted (no chain to fall back on).
    if os.environ.get("AIFORGE_RESEARCHER_SKIP", "0") in ("1", "true"):
        return True, "env_skip"

    if os.environ.get("AIFORGE_RESEARCHER_FORCE", "0") in ("1", "true"):
        return False, "forced_on"

    if _has_reference_pattern(body or ""):
        return False, "has_reference_word"

    from aiforge_core.runtime import request_context
    repo_root = repo_root or os.path.expanduser(
        request_context.get_repo_root() or "~/aiforge_workspace",
    )
    for keyword in _project_keywords(title or ""):
        if _git_log_has_keyword(repo_root, keyword):
            return False, "git_log_match"

    # Greenfield — no reference words AND no git history hits any
    # project keyword from the title. Skip safely.
    return True, "greenfield"


__all__ = ["should_skip_researcher"]
