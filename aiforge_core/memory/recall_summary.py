"""Map→summarize recall: fold many scattered hits into one compact briefing.

``unified_query`` returns ranked hits from ~10 sources. For a topic that pulls a
lot of scattered material, dumping all the snippets into a turn's context is
noisy and token-heavy. This module does the "gather the little stuff, summarize,
hand the LLM one brief" step the recall path was missing: given the query + its
hits, one cheap LLM call synthesizes a compact briefing of only what's relevant.

Config: ``AIFORGE_UMEM_SUMMARIZE`` (default on), ``AIFORGE_UMEM_SUMMARIZE_MIN``
(min hits to bother folding, default 5). Below the threshold — or on ANY
failure — returns ``""`` so the caller keeps its raw ranked list. Never raises.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.recall_summary")

_SUM_SYS = (
    "You synthesize retrieved memory snippets into ONE compact briefing for the "
    "query. Output ONLY 3-8 markdown bullets covering what is RELEVANT to the "
    "query: merge duplicates/paraphrases, DROP hits that don't bear on the "
    "query, and keep concrete facts, decisions, ids, paths, numbers. Do not "
    "invent anything not in the snippets. If nothing is relevant, output nothing."
)


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def summarize_hits(query, hits, *, role: str = "learner",
                   max_chars: int = 1400) -> str:
    """Fold ``hits`` into one compact briefing string, or ``""`` to signal the
    caller to keep its raw list (disabled, too few hits, or LLM failure)."""
    if os.environ.get("AIFORGE_UMEM_SUMMARIZE", "1") in ("0", "false", "no"):
        return ""
    rows = [h for h in (hits or []) if isinstance(h, dict)]
    min_n = max(2, _int_env("AIFORGE_UMEM_SUMMARIZE_MIN", 5))
    if len(rows) < min_n:
        return ""
    snippets: list[str] = []
    for h in rows[:20]:
        body = (h.get("text") or h.get("body") or h.get("summary") or "").strip()
        if body:
            # Carry the SCOPE so the fold can tell a global/shared fact from a
            # repo-specific one (both surface under a repo query); without it the
            # summary presents them as equally authoritative.
            _rp = (h.get("repo") or "").strip()
            _scope = "global" if _rp in ("", "shared") else _rp
            snippets.append(
                f"- [{h.get('source', '?')}·{_scope}] {body[:400]}")
    if len(snippets) < min_n:
        return ""
    payload = f"QUERY: {query}\n\nRETRIEVED SNIPPETS:\n" + "\n".join(snippets)
    try:
        from aiforge_core.llm import client as _llm
        out = _llm.complete(
            role,
            [{"role": "system", "content": _SUM_SYS},
             {"role": "user", "content": payload[:8000]}],
            max_tokens=500, temperature=0.0)
    except Exception as exc:  # noqa: BLE001 — model down → caller keeps raw list
        log.debug("recall summarize failed: %s", exc)
        return ""
    return (out or "").strip()[:max_chars]


__all__ = ["summarize_hits"]
