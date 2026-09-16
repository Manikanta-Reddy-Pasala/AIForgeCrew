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
# The gate errs toward KEEPING: a fragment that slips through is caught again by
# compaction, but a real fact rejected here is silent data loss. So the floor is
# only what cannot possibly be a claim — a single bare word ("Final"). Two words
# is already a subject and something said about it ("svc: alpha"). Character
# length is not a signal at all; every junk string this exists for is caught by
# the scaffolding/request/dangling rules instead. Both knobs stay for an
# operator who wants them stricter.
_MIN_CHARS_DEFAULT = 0
_MIN_WORDS_DEFAULT = 2


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
    re.compile(r"^\s*\|"),                                  # a table row
    # A leading bracket is a table/usage fragment ("[ -c | --clear-l") UNLESS
    # it is a tag or ticket key followed by prose ("[ONE-321] the GSTR session
    # was never renewed").
    re.compile(r"^\s*\[(?![A-Za-z0-9][\w.-]*\]\s+\S)"),
    re.compile(r"^\s*(?:#{1,6}|-{3,}|={3,}|\*{3,})\s"),     # heading / rule
    re.compile(r"^\s*\*\*continued\s+in", re.IGNORECASE),   # compaction artifact
    re.compile(r"^\s*\[[^\]]*\]\([^)]*\)\s*$"),             # a bare md link
)

# A question or a request is an INSTRUCTION, not a durable fact. The distiller
# is supposed to restate what was learned; storing the prompt verbatim is what
# produced "can you add gitlab ci file for this repo".
#
# Only leads that are unambiguously a REQUEST. A bare auxiliary ("is", "are",
# "will", "do") leads plenty of plain statements — "Is-a relationships are
# modelled as INFERRED edges", "Will Smith owns the kubeconfig rotation" — and
# a real question already ends in "?", which is checked separately. The
# trailing \s matters: without it "Is-a" trips the "is" branch.
_REQUEST_RE = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+(?:you|we|u)\b"
    r"|please|pls|kindly|let'?s|lets|help\s+me|i\s+need\s+you\s+to)",
    re.IGNORECASE)

# Leads with a bare pronoun/connective and therefore only means something inside
# the conversation it came from ("this is the data architect problem",
# "attahced his solution", "also we need another service…").
#
# Deliberately NOT here: these/those/their/there/here/such/same/its. Those lead
# perfectly good facts that name their subject in the next word ("These retries
# are capped at 3", "There are exactly two deployment modes", "Same-origin
# policy blocks the fetch") — and this reason also DELETES during repair, so a
# lexical accident like "Same-origin" must not qualify.
_DANGLING_RE = re.compile(
    r"^\s*(?:"
    # a pronoun standing IN for the subject — "this is the data architect
    # problem". A determiner ("This repo's CI runs on Tekton", "That deployment
    # uses…") names its subject in the next word and is a fine fact.
    r"(?:this|that|it|these|those)\s+(?:is|was|are|were|has|have|had|will|"
    r"would|should|means|does|did|can|comes|goes|needs|looks|seems|gets)\b"
    r"|(?:he|him|his|she|her|they|them|their)\s"
    r"|(?:also|and|but|so|then|attached|attahced)\s"
    r")",
    re.IGNORECASE)

# Ends mid-thought: an ellipsis, or a dangling conjunction. Deliberately NOT
# articles/prepositions — a line ending "rule a" or "part of" reads as truncated
# to a regex but is usually a terse label, and rejecting those cost real facts.
_TRUNCATED_TAIL_RE = re.compile(
    r"(?:\.\.\.|…|\b(?:and|or|but|with|that|which|because|so\s+that)\s*)$",
    re.IGNORECASE)

# Every scaffold rule above keys on the MARKUP, so stripping one character
# defeats it: "### Gateway access" is caught, "Gateway access" is not. These
# catch the same things by SHAPE.
#: A section label: a short noun phrase naming a part of a document.
_HEADING_WORDS = frozenset({
    "summary", "overview", "introduction", "intro", "conclusion", "background",
    "steps", "step", "notes", "note", "appendix", "references", "contents",
    "access", "usage", "options", "parameters", "example", "examples",
    "prerequisites", "requirements", "installation", "setup", "final",
    "continued", "part", "next", "previous", "todo", "tbd",
})
_CONTINUED_RE = re.compile(r"^\s*continued\s+in\b", re.IGNORECASE)
_PART_RE = re.compile(r"^\s*part\s+\d+\s*$", re.IGNORECASE)
#: A log line: it records one moment, not something true afterwards.
_LOG_LINE_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
#: Conversational acknowledgement — a turn, never a fact.
_ACK_RE = re.compile(
    r"^\s*(?:ok|okay|yes|yeah|yep|no|nope|thanks|thank\s+you|thx|sure|done|"
    r"great|perfect|nice|cool|got\s+it|makes\s+sense|understood|will\s+do)\b"
    r"[\s\w!.,]*$", re.IGNORECASE)


def _is_section_label(text: str) -> bool:
    """A bare heading: few words, all of them label vocabulary or capitalised,
    and no verb to make it a claim. "Gateway access", "Final Summary",
    "Next Steps" — true of nothing, so nothing to remember."""
    toks = [w.strip(".,:;") for w in text.split() if w.strip(".,:;")]
    if not toks or len(toks) > 4:
        return False
    lowered = [w.lower() for w in toks]
    if not any(w in _HEADING_WORDS for w in lowered):
        return False
    # "Part 2 of the migration runs on the nuc" has a verb and keeps going;
    # a label does not. Capitalisation is the other half of the signal.
    return all(w[0].isupper() or w.lower() in _HEADING_WORDS
               or w.isdigit() for w in toks)


_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
#: An escaped bracket is a literal character — a fact quoting a regex character
#: class (r"^\s*[|\[]") is not an unclosed delimiter.
_ESCAPED_RE = re.compile(r"\\.")


def _unbalanced(text: str) -> bool:
    """True when a bracket is left OPEN — the mark of a line cut out of a
    larger block ("[ c | clear lockout] [ s | setup parameters] [").

    Two things it deliberately does not do, because this reason also DELETES
    during repair:
    * a closer with no opener is NOT unbalanced. "1) commons, 2) MongoDbService"
      and "the runner exits 0 even when pytest fails :)" are ordinary prose.
    * brackets inside a code span do not count, and an odd number of backticks
      is not itself a fault ("A single backtick ` starts a command substitution",
      "the regex is `^\\s*[|\\[]`").
    """
    stripped = _ESCAPED_RE.sub("", _CODE_SPAN_RE.sub("", text or ""))
    depth = 0
    closers = {c: o for o, c in _OPENERS.items()}
    for ch in stripped:
        if ch in _OPENERS:
            depth += 1
        elif ch in closers:
            depth = max(0, depth - 1)      # an unopened closer is not a fault
    return depth > 0


#: CJK writes without spaces, so whitespace tokens under-count it; each
#: ideograph carries about as much as a word.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
#: Any script's letters — a fact in Russian or Japanese is still a fact.
_LETTER_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def _word_count(text: str) -> int:
    t = (text or "").strip()
    cjk = len(_CJK_RE.findall(t))
    if cjk >= 2:
        return max(cjk, len([w for w in re.split(r"\s+", t) if w]))
    return len([w for w in re.split(r"\s+", t) if w])


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
    if _is_section_label(t) or _CONTINUED_RE.match(t) or _PART_RE.match(t):
        out.append("a section label, not a claim")
    if _LOG_LINE_RE.match(t):
        out.append("a log line, not a claim")
    if _ACK_RE.match(t):
        out.append("an acknowledgement, not a claim")
    if _TRUNCATED_TAIL_RE.search(t):
        out.append("truncated mid-thought")
    if _unbalanced(t):
        out.append("unbalanced brackets")
    if not _LETTER_RE.search(t) and not _CJK_RE.search(t):
        out.append("no words")
    return out


#: Reasons that are a matter of DEGREE or inference rather than shape. They
#: belong at the write door (where the distiller can be told to do better) but
#: must not retroactively delete someone's existing note — a terse line a human
#: wrote by hand is still theirs. "truncated mid-thought" is an INFERENCE from a
#: trailing article/conjunction, and it does read a legitimate terse line
#: ("svc: rule a") as truncated, so deleting on it would be guessing.
_SOFT_REASONS = frozenset({"too short", "truncated mid-thought"})


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
#: Tokens that LOOK like an identifier but name nothing in particular. Folding
#: on one of these buckets unrelated facts into a single note — every fact
#: mentioning SELECT would land in a note titled "SELECT".
_GENERIC_SUBJECTS = frozenset({
    "e.g", "eg", "i.e", "ie", "etc", "vs", "aka", "n/a", "and/or", "24/7",
    "select", "insert", "update", "delete", "from", "where", "join", "null",
    "true", "false", "none", "get", "post", "put", "patch", "head", "options",
    "api", "url", "uri", "http", "https", "json", "yaml", "xml", "csv", "sql",
    "todo", "fixme", "note", "warn", "warning", "error", "info", "debug",
    "ok", "id", "ids", "key", "value", "name", "type", "file", "path",
})
#: A number, a version, a port — a VALUE the fact is about something else with.
_VALUE_TOKEN_RE = re.compile(r"^[\d.,:/_-]+$")
_SUBJECT_MAX = 70


def _usable_subject(tok: str) -> bool:
    """A token is a subject only if it names something specific."""
    t = (tok or "").strip().strip(".,;:!?")
    if not t or len(t) > _SUBJECT_MAX:
        return False
    if _VALUE_TOKEN_RE.match(t):
        return False
    return t.lower() not in _GENERIC_SUBJECTS


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
    for span in re.findall(r"`([^`]{2,60})`", t):
        # A backticked VALUE (`50`, `true`) is what the fact says, not what it
        # is about — folding on it merges every fact that mentions 50.
        if _usable_subject(span.strip()):
            return span.strip()
    toks = _candidate_tokens(t)
    for w in toks:
        if len(w) > 2 and _CODEY_RE.match(w) and not w.endswith(".") \
                and _usable_subject(w):
            return w
    for w in toks:
        if len(w) > 2 and _IDENT_RE.match(w) and _usable_subject(w) and (
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


def looks_truncated(text: str) -> bool:
    """True when ``text`` visibly stops mid-thought — an ellipsis, a dangling
    conjunction, or an unclosed bracket. This is what a truncation-ladder rung
    looks like, as opposed to a short but COMPLETE fact."""
    t = (text or "").strip()
    return bool(t) and (bool(_TRUNCATED_TAIL_RE.search(t)) or _unbalanced(t))


def supersedes(new: str, old: str) -> bool:
    """True when ``new`` is the finished version of the FRAGMENT ``old``.

    The ladder in the wild (``[ c | clear l`` → ``[ c | clear lockout] [ s |
    setup`` → the full line) is a prefix chain, and collapsing it is the point.
    But a bare prefix test is silent data loss, because superseding DELETES the
    older claim with no archive:

    * ``JetStream batch size is 50`` is a character prefix of ``JetStream batch
      size is 500 for the DLQ replay`` — two different numbers, and the true
      one would vanish. Hence the word boundary.
    * ``svc: rule a`` is a prefix of ``svc: rule applies to admins``, and
      ``OrderController maps /orders`` of ``... /orders-v2 to the legacy
      handler`` — both COMPLETE facts about different things. Hence: only a
      claim that visibly stops mid-thought can be superseded at all.

    Everything else is kept as a separate claim. A duplicate note is cheap; a
    deleted fact is not recoverable.
    """
    a, b = claim_key(new), claim_key(old)
    if not (a and b) or a == b:
        return False
    # No word boundary required: a real ladder rung is cut MID-WORD ("[ c |
    # clear l"). looks_truncated is what keeps that safe — without it,
    # "batch size is 50" is a prefix of "batch size is 500 for the DLQ job"
    # and the true fact about 50 would be deleted.
    return a.startswith(b) and looks_truncated(old)


def title_for(subject: str, claim: str) -> str:
    """Notes are titled by SUBJECT (never ``claim[:70]``) so the filename stays
    stable across updates and the subject upsert can find it again."""
    s = re.sub(r"\s+", " ", (subject or "").strip()) or derive_subject(claim)
    return s[:70]


__all__ = ["claim_key", "derive_subject", "gate_enabled", "is_wellformed",
           "issues", "looks_truncated", "strong_subject", "structural_issues",
           "supersedes", "title_for"]
