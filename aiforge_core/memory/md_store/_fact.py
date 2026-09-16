"""md_store internals: what makes a captured string an actual FACT.

`capture()` used to accept any non-empty string and title it `text[:70]`, so a
CLI help fragment (``[ -c | --clear-l``), a heading (``| GET /api/version |``),
a compaction artifact (``**Continued in:** [part 2]``) and a raw user turn
("can you do chnages") all became permanent memories. This module is the single
place that decides "is this a fact?", what SUBJECT it is about, and how two
phrasings of the same fact compare — pure functions, no I/O (that lives in
`_subject` / `_ingest`).
"""
from __future__ import annotations

import os
import re

# ── Tunables (env-overridable; the gate is ON by default) ────────────────────
# A real fact can be terse ("OrderController maps /orders", "svc: rule one") —
# three words is subject + verb + object, and fewer than that is a label, not a
# claim. Character length is NOT a signal: every junk string this gate exists
# for is caught by the scaffolding/request/dangling/truncation rules, and a
# character floor only ever cost us real terse facts. The knob stays for an
# operator who wants one; it is off by default.
_MIN_CHARS_DEFAULT = 0
_MIN_WORDS_DEFAULT = 3


def _i_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def gate_enabled() -> bool:
    """False restores the legacy "any non-empty string is a memory" behaviour."""
    return os.environ.get("AIFORGE_MEMORY_FACT_GATE", "1").strip().lower() not in (
        "0", "off", "false", "no")


# ── Reject patterns ──────────────────────────────────────────────────────────
# Scaffolding: table rows, CLI usage fragments, headings, rules, and the
# compactor's own continuation marker — none of these are claims about anything.
_SCAFFOLD_RES = (
    re.compile(r"^[|\[\]{}<>#*_=~+\-–—·•`'\"\s]*$"),        # punctuation only
    re.compile(r"^\s*[|\[]"),                               # leads with | or [
    re.compile(r"^\s*(?:#{1,6}|-{3,}|={3,}|\*{3,})\s"),     # heading / rule
    re.compile(r"^\s*\*\*continued\s+in", re.IGNORECASE),   # compaction artifact
    re.compile(r"^\s*\[[^\]]*\]\([^)]*\)\s*$"),             # a bare md link
)

# A question or a request is an INSTRUCTION, not a durable fact. The distiller
# is supposed to restate what was learned; storing the prompt verbatim is what
# produced "can you add gitlab ci file for this repo".
_REQUEST_RE = re.compile(
    r"^\s*(?:can|could|would|will|should|shall|do|does|did|is|are|was|were|"
    r"please|pls|kindly|let'?s|lets|help\s+me|i\s+need\s+you\s+to)\b",
    re.IGNORECASE)

# Leads with a pronoun/connective and therefore only means something inside the
# conversation it came from ("this is the data architect problem",
# "attahced his solution", "also we need another service…").
_DANGLING_RE = re.compile(
    r"^\s*(?:this|that|these|those|it|its|he|him|his|she|her|they|them|their|"
    r"also|and|but|so|then|there|here|such|same|above|below|attached|attahced)\b",
    re.IGNORECASE)

# Ends mid-thought: an ellipsis, or a trailing word that cannot end a sentence.
_TRUNCATED_TAIL_RE = re.compile(
    r"(?:\.\.\.|…|\b(?:and|or|but|the|a|an|to|of|in|on|for|with|that|which|"
    r"from|by|as|at|is|are|was|were|be|been)\s*)$",
    re.IGNORECASE)

_OPENERS = {"(": ")", "[": "]", "{": "}"}


def _unbalanced(text: str) -> bool:
    """A fragment cut out of a larger block leaves brackets/backticks open."""
    if text.count("`") % 2:
        return True
    depth = {o: 0 for o in _OPENERS}
    closers = {c: o for o, c in _OPENERS.items()}
    for ch in text:
        if ch in _OPENERS:
            depth[ch] += 1
        elif ch in closers:
            depth[closers[ch]] -= 1
    return any(v != 0 for v in depth.values())


def _word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def issues(text: str) -> list[str]:
    """Every reason ``text`` is not a durable fact (empty list = it is one)."""
    t = (text or "").strip()
    if not t:
        return ["empty"]
    out: list[str] = []
    if len(t) < _i_env("AIFORGE_MEMORY_FACT_MIN_CHARS", _MIN_CHARS_DEFAULT):
        out.append("too short")
    if _word_count(t) < _i_env("AIFORGE_MEMORY_FACT_MIN_WORDS", _MIN_WORDS_DEFAULT):
        out.append("too few words")
    if any(rx.search(t) for rx in _SCAFFOLD_RES):
        out.append("scaffolding, not a claim")
    if t.endswith("?") or _REQUEST_RE.match(t):
        out.append("a request/question, not a fact")
    if _DANGLING_RE.match(t):
        out.append("leads with a dangling reference (no subject)")
    if _TRUNCATED_TAIL_RE.search(t) or _unbalanced(t):
        out.append("truncated mid-thought")
    if not re.search(r"[A-Za-z]{3}", t):
        out.append("no words")
    return out


#: Reasons that are a matter of DEGREE rather than shape. They belong at the
#: write door (where the distiller can be told to do better) but must not
#: retroactively delete someone's existing note — a terse line that a human
#: wrote by hand is still theirs.
_SOFT_REASONS = frozenset({"too short"})


def structural_issues(text: str) -> list[str]:
    """Only the reasons that make ``text`` structurally not-a-claim: scaffolding,
    a dangling reference, a request, a truncation. Used by the repair pass, which
    DELETES, so it holds a higher bar than the write gate."""
    return [r for r in issues(text) if r not in _SOFT_REASONS]


def is_wellformed(text: str) -> tuple[bool, list[str]]:
    """``(ok, reasons)``. With the gate off every non-empty string passes."""
    reasons = issues(text)
    if not gate_enabled():
        return bool((text or "").strip()), reasons
    return not reasons, reasons


# ── Subject derivation ───────────────────────────────────────────────────────
# A fact is ABOUT something. The subject is what the note is keyed on, so two
# facts about `MessageRetryService` land in one note instead of two files.
_STOP = frozenset(
    "a an the this that these those it its is are was were be been being to of "
    "in on for with and or but if then when we you i they he she our your their "
    "not no do does did can could should would will shall must may might has "
    "have had there here as at by from into over under about after before while "
    "user users use used using new now only also very just".split())

_CODEY_RE = re.compile(r"^[\w./\\:-]*[/._\\][\w./\\:-]*$")   # path / dotted name
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _candidate_tokens(text: str) -> list[str]:
    return [w.strip(".,;:!?()[]{}\"'") for w in re.split(r"\s+", text.strip()) if w]


def strong_subject(text: str) -> str | None:
    """A subject we are CONFIDENT about: a quoted identifier, a path/dotted name,
    or a CamelCase/ALLCAPS token — something the text names outright.

    Only a strong subject may merge two captures into one note. The weak
    fallback below is a guess good enough to TITLE a note, but merging on a
    guess buckets unrelated facts together ("svc: rule a" and "svc: rule b"
    both reduce to "svc rule"), and a wrong merge is not recoverable.
    """
    t = (text or "").strip()
    if not t:
        return None
    backticked = re.findall(r"`([^`]{2,60})`", t)
    if backticked:
        return backticked[0].strip()
    toks = _candidate_tokens(t)
    for w in toks:
        if len(w) > 2 and _CODEY_RE.match(w) and not w.endswith("."):
            return w
    for w in toks:
        if len(w) > 2 and _IDENT_RE.match(w) and (
                re.search(r"[a-z][A-Z]", w) or (w.isupper() and len(w) > 2)):
            return w
    return None


def derive_subject(text: str) -> str:
    """Best-effort "what is this about" for a fact the caller did not label —
    the strong subject when there is one, else the leading non-stopwords."""
    t = (text or "").strip()
    if not t:
        return "note"
    strong = strong_subject(t)
    if strong:
        return strong
    toks = _candidate_tokens(t)
    head = [w for w in toks if w.lower() not in _STOP][:4]
    return " ".join(head) or toks[0]


# ── Comparison keys ──────────────────────────────────────────────────────────
def claim_key(text: str) -> str:
    """Normalised form used to tell two phrasings of one claim apart."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def supersedes(new: str, old: str) -> bool:
    """True when ``new`` is a strictly fuller version of ``old``.

    The truncation ladder in the wild (``[ c | clear l`` → ``[ c | clear
    lockout] [ s | setup`` → the full line) is exactly a prefix chain, so the
    longest member replaces the rest instead of piling up three notes.
    """
    a, b = claim_key(new), claim_key(old)
    return bool(a) and bool(b) and a != b and a.startswith(b)


def title_for(subject: str, claim: str) -> str:
    """Notes are titled by SUBJECT (never ``claim[:70]``) so the filename stays
    stable across updates and the subject upsert can find it again."""
    s = re.sub(r"\s+", " ", (subject or "").strip()) or derive_subject(claim)
    return s[:70]


__all__ = ["claim_key", "derive_subject", "gate_enabled", "is_wellformed",
           "issues", "strong_subject", "structural_issues", "supersedes",
           "title_for"]
