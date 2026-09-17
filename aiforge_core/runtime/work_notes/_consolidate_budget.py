"""Fitting a consolidation into the model context: token budgets, ranking,
splitting and exact unions."""
from __future__ import annotations

import json
import re


def _pkg():
    """``_consolidate``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``_consolidate``; patch any other
    name on this module."""
    import aiforge_core.runtime.work_notes._consolidate as package
    return package


# ── context budgeting ─────────────────────────────────────────────────────
#
# A fold sends {current_sections, new_information} and asks for the WHOLE
# consolidated note back. Both halves grow: `current_sections` accumulates every
# fact kept so far, and the output request was sized from that same payload. On
# a 2,163-node mesh fold this reached 229,377 input tokens with 32,768 output
# requested against a 262,144 window — 262,145 total, over by ONE token, and
# every retry failed identically because nothing shrank.
#
# So both sides are budgeted here, against the real window:
#   * what we SEND is capped, and whatever does not fit is held back and merged
#     deterministically afterwards — bounded call, no lost facts;
#   * what we ASK FOR is whatever the window has left, never a fixed ceiling.

# Chars per token. Deliberately pessimistic (real English is ~4): a fold that
# under-estimates its prompt gets a 400 and loses the whole group, while one
# that over-estimates just does an extra chunk.
_CHARS_PER_TOKEN = 3

# Leave room for the system prompt, the JSON scaffolding and tokeniser
# disagreement. The failure above was a ONE-token overshoot, so the slack is
# what stops arithmetic that is merely almost right.
_CTX_SLACK_TOKENS = 2048

# Smallest useful completion. Below this the model cannot return even a trimmed
# note, so the caller must shrink the input instead of making a doomed call.
_MIN_OUTPUT_TOKENS = 1024


def _ctx_window(role: str) -> int:
    """Context window for ``role``, in tokens.

    Asks the router, which already resolves per-role env → runtime settings →
    default, so the operator's configured window is honoured rather than a
    second constant drifting alongside it.
    """
    import os as _os

    raw = (_os.environ.get("AIFORGE_CONSOLIDATE_CTX_TOKENS") or "").strip()
    if raw:
        try:
            return max(8192, int(raw))
        except ValueError:
            pass
    try:
        from aiforge_core.llm.router import _local_ctx_window
        return max(8192, int(_local_ctx_window(role)))
    except Exception:  # noqa: BLE001 — a missing router must not stop compaction
        return 32768


def input_char_budget(role: str, *, output_tokens: int = _MIN_OUTPUT_TOKENS) -> int:
    """Chars of prompt ``role`` can take while still leaving room to answer.

    Public because the brief summariser in ``md_store`` needs the SAME rule.
    It used to carry its own 28,000-char constant, which is a guess about a
    window rather than a reading of it: too small for a 262k model (needless
    map-reduce passes) and too large the moment someone points a role at a 32k
    one.
    """
    window = _pkg()._ctx_window(role)
    return max(4000, (window - output_tokens - _CTX_SLACK_TOKENS) * _CHARS_PER_TOKEN)


def _est_tokens(text: str) -> int:
    return max(1, len(text or "") // _CHARS_PER_TOKEN)


def _sections_chars(sections: dict) -> int:
    return len(json.dumps(sections, ensure_ascii=False))


def _overlap(item: str, focus_words: set) -> int:
    """Cheap lexical overlap between one item and the incoming text.

    Word-set intersection, no model and no embedding: this runs per item per
    fold, and the job is only to order the slice — being roughly right is worth
    far more here than being slowly precise.
    """
    if not focus_words:
        return 0
    words = set(re.findall(r"[a-z0-9]{4,}", item.lower()))
    return len(words & focus_words)


def _ranked_pool(items: list, focus_words: set, turn: int) -> list:
    """One section's items, ordered by how likely they are to collide with the
    incoming chunk.

    Items sharing vocabulary with ``focus`` come first (a duplicate almost
    always shares words with what it duplicates), then the rest entered at a
    ROTATING offset derived from ``turn`` — newest-first within the rotation,
    since recent items are the likeliest collisions after the relevant ones.
    """
    if not items:
        return []
    scored = {i: _overlap(str(it), focus_words) for i, it in enumerate(items)}
    relevant = sorted((i for i, sc in scored.items() if sc > 0),
                      key=lambda i: (-scored[i], -i))   # best match, then newest
    rest = [i for i, sc in scored.items() if sc == 0]
    if rest:
        off = turn % len(rest)
        rest = list(reversed(rest[off:] + rest[:off]))
    return [items[i] for i in relevant + rest]


def _fill_budget(pools: dict, used: int, budget_chars: int) -> tuple[dict, dict]:
    """Round-robin across sections so one huge list cannot starve the others.
    ``(chosen, overflow)``."""
    chosen = {k: [] for k in pools}
    overflow = {k: [] for k in pools}
    while any(pools.values()):
        for k in ("facts", "learnings", "key_results", "links"):
            if not pools[k]:
                continue
            item = pools[k].pop(0)
            cost = len(str(item)) + 4
            if used + cost > budget_chars:
                overflow[k].append(item)
            else:
                chosen[k].append(item)
                used += cost
    return chosen, overflow


def _split_state(sections: dict, budget_chars: int, *, focus: str = "",
                 turn: int = 0) -> tuple[dict, dict]:
    """Split accumulated sections into (sent, held) under ``budget_chars``.

    The model needs the accumulated state to dedupe against, but it does not
    need ALL of it to fold one chunk — and sending all of it is what blew the
    window. What it does need is the part that THIS chunk is likely to collide
    with, so the slice is CHOSEN rather than merely truncated (see
    :func:`_ranked_pool`).

    The rotation is what stops the slice being the same items every time. A
    plain newest-first cut showed the model the tail on every chunk, so genuinely
    old state was never re-examined and a near-duplicate of something ancient
    could survive forever. Rotating means that across a group's chunks every
    item eventually gets its turn in front of the model, while any one call
    stays bounded.

    Whatever does not fit is held back and re-unioned by the caller, so this
    bounds the CALL without dropping a single item.
    """
    objective = sections.get("objective") or ""
    focus_words = set(re.findall(r"[a-z0-9]{4,}", (focus or "").lower()))
    keys = ("key_results", "facts", "links", "learnings")
    pools = {k: _ranked_pool(list(sections.get(k) or []), focus_words, turn)
             for k in keys}
    chosen, overflow = _fill_budget(pools, len(objective), budget_chars)

    # Emit the slice in the sections' own order, not selection order: the prompt
    # reads as a note, and a shuffled one invites the model to "fix" the order.
    sent = {"objective": objective}
    held = {}
    for k in keys:
        picked = set(map(str, chosen[k]))
        over = set(map(str, overflow[k]))
        sent[k] = [it for it in (sections.get(k) or []) if str(it) in picked]
        held[k] = [it for it in (sections.get(k) or []) if str(it) in over]
    return sent, held


def _union_exact(first, second) -> list[str]:
    """``first`` then whatever of ``second`` it does not already contain.

    Exact (case/space-insensitive) keys only — deliberately NOT ``_dedupe_ci``.
    That one also drops a short item CONTAINED in a longer one, which is right
    when distilling a section but wrong here: this runs over items that were
    already deduped once, and re-applying containment across the join silently
    ate survivors ("a" vanishing because "brand new fact" contains an "a").
    Merging back what the model never saw must add, never subtract.
    """
    pkg = _pkg()
    out = list(first or [])
    seen = {pkg._ci_key(str(x)) for x in out}
    for item in (second or []):
        k = pkg._ci_key(str(item))
        if k and k not in seen:
            seen.add(k)
            out.append(item)
    return out


def _union_sections(a: dict, b: dict) -> dict:
    """Union of two section dicts, ``a`` first, adding only what ``b`` uniquely
    holds. See :func:`_union_exact` for why this does not re-dedupe."""
    return {
        "objective": (a.get("objective") or b.get("objective") or "").strip(),
        "key_results": _union_exact(a.get("key_results"), b.get("key_results")),
        "facts": _union_exact(a.get("facts"), b.get("facts")),
        "links": _union_exact(a.get("links"), b.get("links")),
        "learnings": _union_exact(a.get("learnings"), b.get("learnings")),
    }
