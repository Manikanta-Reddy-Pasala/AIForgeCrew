"""Claim-vs-reality guard — catch hallucinated file edits.

A local model, especially once the context has been condensed, sometimes
NARRATES a file edit ("I have now applied the fix to Dashboard.vue", "Confirmed
Fixes Applied", pasting 'Current File Content') without ever emitting a write
ACTION — so nothing lands on disk, yet the user is told it did. This module is
the detection half:

* :func:`_claims_file_edits` — does a final answer ASSERT it edited a file?
* :func:`_worktree_fingerprint` — did the working tree actually change?

The loop combines the two (claim asserted + zero landed edits + tree unchanged
→ nudge, then annotate) — see the ``final`` branch in ``_loop.py``. Kept
separate from ``_verify.py`` (test/build gate) by concern.
"""
from __future__ import annotations

import os
import re

# An edit VERB in the past tense — the model saying it already did the write.
# NOTE: "made" is NOT here — it co-occurs with file nouns in ordinary prose ("I
# made sure the file exists"); a this-turn "made the changes" is caught by
# _MADE_CHANGES_RE instead.
_EDIT_CLAIM_VERB_RE = re.compile(
    r"(?i)\b(?:applied|committed|wrote|written|saved|updated|modified|edited|"
    r"patched|replaced|inserted|refactored|implemented|created|corrected|"
    r"adjusted|rewrote|overwrote|added|removed|deleted|fixed|renamed|appended)\b")

# The specific this-turn phrasing "I made the changes / we've made some edits" —
# a claim even though "made" is too promiscuous to be a general edit verb.
# Requires a FIRST-PERSON subject so "the author made the changes" (third party)
# and "once you've made the changes" (instruction to the user) don't fire.
_MADE_CHANGES_RE = re.compile(
    r"(?i)\b(?:i|we)(?:'ve|'ll| have| will)?\s+"
    r"(?:just\s+|now\s+|also\s+|already\s+|then\s+)?made\s+"
    r"(?:the\s+|some\s+|a\s+few\s+|these\s+)?"
    r"(?:changes?|edits?|updates?|modifications?|fixes?|adjustments?)\b")

# A THIRD-PARTY subject that is the one DOING the edit — a commit / author /
# PR, not the assistant. Only these dependable subject nouns (NOT ordinary
# content words like "diff"/"patch"/"snippet"/"example" that appear as OBJECTS
# in genuine claims). Used with an adjacency check (:func:`_subject_is_descriptive`)
# so it suppresses only when the noun actually precedes the edit verb.
_DESCRIPTIVE_NOUN_RE = re.compile(
    r"(?i)(?:\b(?:this|that|the|a|another|the\s+following)\s+"
    r"(?:\w+\s+){0,2}"          # optional adjectives ("the recent commit")
    r"(?:commit|pull\s+request|pr|mr|changeset|revision|author|contributor|"
    r"developer|maintainer|linter|formatter|compiler|tool|script|"
    r"pipeline|ci|bot|migration|engineer|team|user|colleague|reviewer)"
    # bare third-party subjects (no determiner)
    r"|\b(?:someone|somebody|anyone|everyone|nobody|they|he|she))\b")

# PASSIVE-VOICE auxiliary — "the file WAS deleted", "the config IS updated" — the
# subject is not the assistant asserting a this-turn write.
_PASSIVE_RE = re.compile(r"(?i)\b(?:was|were|been|being|is|are|be|get|got|gets)\b")

# ADVISORY / MODAL / PASSIVE framing — the clause RECOMMENDS an edit ("the config
# should be updated", "a new file needs to be created", "must be removed") rather
# than CLAIMING one happened. The edit verb-lemmas are identical to the past
# tense the guard looks for, so without this a suggestion trips the guard.
_ADVISORY_RE = re.compile(
    r"(?i)\b(?:should|shall|must|ought to|need to|needs to|have to|has to|"
    r"to be|can be|could be|will be|would be|may be|might be|"
    r"recommend|suggest|consider|make sure|please)\b")

# An OBJECT that makes the verb about a file/code artifact — a path with an
# extension, an explicit file/code noun, or the screenshot's "fixes applied"
# heading. Required ALONGSIDE the verb so prose like "I updated my estimate"
# doesn't trip it.
_EDIT_CLAIM_OBJ_RE = re.compile(
    r"(?i)(?:\bthe\s+(?:file|code|function|method|component|class|config|"
    r"module|script|change)s?\b|\bfiles?\b|\bfix(?:es)?\s+applied\b|\bchanges?\s+"
    r"(?:applied|made|written|committed|saved)\b|[\w./-]+\.(?:py|js|jsx|ts|tsx|"
    r"vue|java|kt|kts|go|rs|c|cc|cpp|h|hpp|rb|php|cs|swift|scala|sh|bash|yaml|"
    r"yml|json|xml|html|css|scss|md|sql|toml|gradle|dockerfile|cfg|ini))")


# A PRIOR-WORK cue — the model recapping edits it made in an EARLIER turn, not
# claiming a this-turn write. Deliberately EXCLUDES "already" / "so far": those
# are hallmarks of a this-turn false claim ("I've already applied the fix to
# X.vue"), NOT a recap — treating them as recap cues let hallucinations escape
# the guard. Only unambiguous prior-turn framing suppresses.
_RECAP_CUE_RE = re.compile(
    r"(?i)\b(?:previously|earlier|beforehand|"
    r"in (?:an?|the) (?:prior|previous|earlier) (?:turn|step|message|response)|"
    r"(?:last|prior|previous) turn|earlier in (?:this|the) (?:session|chat|conversation))\b")

# Split into clauses so a suppressor (recap cue / descriptive subject) in ONE
# sentence can't disable the guard for a fresh hallucinated claim in ANOTHER.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# How close (chars) a descriptive noun must sit BEFORE an edit verb to count as
# its subject.
_SUBJECT_WINDOW = 40


# A re-subject boundary — after it, the edit verb belongs to a NEW subject (the
# assistant), not the preceding descriptive noun ("the pull request, I updated…").
_RESUBJECT_RE = re.compile(r"(?i)[,;:]|\b(?:i|we)\b")


def _subject_is_descriptive(clause: str) -> bool:
    """True when a third-party subject (commit / author / …) is the one doing an
    edit verb in this clause — the noun is closely FOLLOWED by an edit verb
    ("this commit added…", "the author removed…"), so the clause DESCRIBES
    someone else's diff rather than claiming a this-turn edit. A descriptive noun
    used only as an OBJECT ("I updated the diff view component"), or one whose
    verb belongs to a RE-SUBJECT ("after the pull request, I updated…"), does NOT
    suppress."""
    for m in _DESCRIPTIVE_NOUN_RE.finditer(clause):
        # If an edit verb (or "made the changes") precedes this noun, the noun is
        # that verb's OBJECT — the assistant's own claim ("I refactored the build
        # script and updated X") — NOT a third-party subject. Only a leading
        # descriptive noun ("The linter removed X") is a real subject.
        head = clause[:m.start()]
        if _EDIT_CLAIM_VERB_RE.search(head) or _MADE_CHANGES_RE.search(head):
            continue
        tail = clause[m.end(): m.end() + _SUBJECT_WINDOW]
        cut = _RESUBJECT_RE.search(tail)      # stop before a new subject takes over
        if cut:
            tail = tail[:cut.start()]
        if _EDIT_CLAIM_VERB_RE.search(tail):
            return True
    return False


# A NEGATION marker — the edit verb is DENIED, not claimed ("I haven't updated
# the file", "I did not change X.py"). Must sit just before the verb.
_NEGATION_RE = re.compile(
    r"(?i)\b(?:not|never|no|n't|didn't|don't|doesn't|haven't|hasn't|hadn't|"
    r"won't|wouldn't|can't|cannot|couldn't|wasn't|weren't|isn't|aren't|"
    r"did not|do not|does not|have not|has not|had not|will not|would not|"
    r"could not|can not|was not|were not|is not|are not)\b")


def _advisory_before(clause: str, pos: int) -> bool:
    """True when an advisory/modal marker OR a negation sits just BEFORE the edit
    verb at ``pos`` — i.e. the verb is FRAMED as a suggestion ("the config should
    be updated") or DENIED ("I haven't updated the file"), not claimed. A stray
    "please"/"not" LATER in the clause ("I updated X, please review" / "I updated
    X, not Y") does not, so a real claim isn't suppressed."""
    # ADJACENCY: the marker must actually PRECEDE this verb closely ("was
    # deleted", "should be updated", "haven't updated") — a 16-char window is
    # ~2-3 words. A stray auxiliary/negation from an unrelated fragment earlier
    # in the same clause ("The bug is fixed, and I updated X") must NOT suppress
    # the real claim, so the window is tight, not the whole clause.
    pre = clause[max(0, pos - 16): pos]
    # …AND a re-subject boundary (comma/semicolon or a fresh "I"/"we") starts a
    # NEW fragment: a marker BEFORE it ("is fixed," / "was failing;") governs the
    # old subject, not this verb — keep only text after the LAST such boundary so
    # "The bug is fixed, and I updated X" reads as a real claim, while a genuine
    # adjacent frame ("I haven't updated X") keeps its marker.
    last = None
    for m in _RESUBJECT_RE.finditer(pre):
        last = m
    if last is not None:
        pre = pre[last.end():]
    return bool(_ADVISORY_RE.search(pre) or _NEGATION_RE.search(pre)
                or _PASSIVE_RE.search(pre))


def _claims_file_edits(text: str) -> bool:
    """True when the answer ASSERTS it edited/created a file THIS turn. Evaluated
    PER CLAUSE so a recap/descriptive sentence can't mask a fresh claim in a
    neighbouring one. A clause is a claim when it has an edit verb + a file/code
    object (or first-person "made the changes") whose verb is NEITHER
    advisory-framed (a suggestion) NOR driven by a third-party subject, and the
    clause carries no prior-work cue. Conservative — a miss (no nudge) is fine;
    the loop's disk cross-check is the real safety net."""
    if not text:
        return False
    for clause in _SENT_SPLIT.split(text):
        if _RECAP_CUE_RE.search(clause):
            continue
        # first-person "I/we made the changes" — a claim unless it's suggested
        # ("we should make the changes") or attributed to a third party.
        made = _MADE_CHANGES_RE.search(clause)
        if (made and not _advisory_before(clause, made.start())
                and not _subject_is_descriptive(clause)):
            return True
        if not _EDIT_CLAIM_OBJ_RE.search(clause):
            continue
        if _subject_is_descriptive(clause):
            continue
        # a claim if ANY edit verb is not framed as a suggestion.
        for vm in _EDIT_CLAIM_VERB_RE.finditer(clause):
            if not _advisory_before(clause, vm.start()):
                return True
    return False


def _edit_claim_guard_enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_EDIT_CLAIM_GUARD", "1") not in ("0", "false")


def _worktree_fingerprint(cwd: str) -> str:
    """A cheap fingerprint of the working tree's dirty state (``git status
    --porcelain``). Compared before/after a turn to prove edits actually LANDED
    on disk regardless of which tool wrote them. Returns ``""`` when not a git
    repo or git is unavailable — callers must treat ``""`` == "no signal", NOT
    "clean" (else a non-git workspace would falsely read as unchanged)."""
    if not cwd:
        return ""
    try:
        import subprocess
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd,
            capture_output=True, text=True, timeout=5)
        return out.stdout if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — a git hiccup must never break the turn
        return ""


def _edit_claim_nudge() -> str:
    """The corrective turn injected when the model claims edits it didn't make.
    Gives a clean escape for a truthful recap so the guard can't loop on it."""
    return (
        "[automated check — not the user] Your message claims you edited / "
        "created / applied changes to a file, but you emitted NO file-write "
        "ACTION this turn and the working tree did not change — so nothing was "
        "written to disk. Do NOT claim edits you did not make. Either emit the "
        "real ACTION (file_write / patch / …) to apply the change NOW, or if you "
        "genuinely cannot, say so plainly. If you already made this change in an "
        "EARLIER turn and are only recapping, say 'previously' explicitly. "
        "Re-read the file first if unsure.")


def _edit_claim_disclaimer(text: str) -> str:
    """Prepend an honest note when the model still asserts an un-backed edit
    after the nudge budget is spent — so the user is never told a change landed
    that didn't."""
    return ("⚠ Note: no file changes were recorded this turn — nothing was "
            "written to disk.\n\n" + (text or ""))
