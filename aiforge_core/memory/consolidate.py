"""Item-level memory consolidation: merge duplicates without a model.

A project brief is an OKF note: bullets under Facts and Learnings, then a
free body. Folding it through a model rewrote every bullet into one prose
summary (the structure was lost, and Learnings older than the last 12 never
reached the model input). This keeps the bullets and merges them:

* exact duplicates — same text once case, punctuation, bullets and a
  ``[topic]`` prefix are folded (``work_notes.item_key``);
* near duplicates — at least ``AIFORGE_MEMORY_DUP_WORDS`` of their words in
  common (default 0.8), or a close lexical vector (hash embedding cosine
  ``>= AIFORGE_MEMORY_DUP_COSINE``, default 0.93) with at least 60% of words
  shared.

The most detailed wording (the longest) of each group is kept, at the place
of the group's first item. Every merge is returned so it can be written to a
readable report. Incremental: items in ``known`` were consolidated with each
other before, so only new items are compared — a re-run costs only what
changed.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z_][a-z0-9_]+|\d+(?:[.:,]\d+)*")
_NUM = re.compile(r"\d+(?:[.:,]\d+)*")


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def item_id(text: str) -> str:
    """A short stable id for an item (its normalized text)."""
    from aiforge_core.runtime.work_notes import item_key
    return hashlib.blake2b(item_key(text).encode(), digest_size=5).hexdigest()


def _words(text: str) -> frozenset:
    return frozenset(_WORD.findall((text or "").lower()))


def _numbers(text: str) -> frozenset:
    return frozenset(_NUM.findall(text or ""))


def _detail(text: str) -> int:
    """How much a wording says: its normalized text (tags, bullets, case and
    punctuation folded), so a "[topic]" prefix is not counted as detail."""
    from aiforge_core.runtime.work_notes import item_key
    return len(item_key(text))


def _jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Result:
    kept: list = field(default_factory=list)
    merges: list = field(default_factory=list)     # [(kept_text, [merged texts])]
    compared: int = 0

    @property
    def removed(self) -> int:
        return sum(len(m) for _k, m in self.merges)


def consolidate(items: list, known: "set | None" = None) -> Result:
    """``items`` (bullet strings, order kept) with duplicates merged."""
    from aiforge_core.memory import local_embed
    from aiforge_core.runtime.work_notes import item_key
    words_t = _f("AIFORGE_MEMORY_DUP_WORDS", 0.8)
    cos_t = _f("AIFORGE_MEMORY_DUP_COSINE", 0.93)
    known = known or set()
    out = Result()
    slots: list = []          # [text, key, words, vec|None, merged list]
    by_key: dict = {}
    index: dict = {}          # word -> slot numbers (candidates share a word)
    for raw in items:
        text = (raw or "").strip()
        k = item_key(text)
        if not k:
            continue
        if k in by_key:                                   # exact duplicate
            s = slots[by_key[k]]
            s[4].append(text)
            if _detail(text) > _detail(s[0]):
                s[4].remove(text)
                s[4].append(s[0])
                s[0] = text
            continue
        w = _words(text)
        nums = _numbers(text)
        hit = None
        if item_id(text) not in known:                   # new: compare
            cands = set()
            for t in w:
                cands.update(index.get(t, ()))
            vec = None
            for n in sorted(cands):
                s = slots[n]
                out.compared += 1
                if nums != _numbers(s[0]):
                    continue            # different numbers are different facts
                j = _jaccard(w, s[2])
                if j >= words_t:
                    hit = n
                    break
                if j >= 0.6:
                    if vec is None:
                        vec = local_embed._hash_embed(k)
                    if s[3] is None:
                        s[3] = local_embed._hash_embed(s[1])
                    if local_embed.cosine(vec, s[3]) >= cos_t:
                        hit = n
                        break
        if hit is not None:
            s = slots[hit]
            s[4].append(text)
            if _detail(text) > _detail(s[0]):              # the most detailed wording
                s[4].remove(text)
                s[4].append(s[0])
                s[0] = text
            by_key[k] = hit
            continue
        n = len(slots)
        slots.append([text, k, w, None, []])
        by_key[k] = n
        for t in w:
            index.setdefault(t, []).append(n)
    out.kept = [s[0] for s in slots]
    out.merges = [(s[0], s[4]) for s in slots if s[4]]
    return out


def report_lines(section: str, res: Result) -> list:
    """Readable lines for a merge report."""
    lines = []
    for kept, merged in res.merges:
        lines.append(f"- **{section}** kept `{item_id(kept)}`: {kept}")
        lines += [f"  - merged: {m}" for m in merged]
    return lines
