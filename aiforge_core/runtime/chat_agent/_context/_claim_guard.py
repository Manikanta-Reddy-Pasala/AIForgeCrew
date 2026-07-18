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
    r"(?i)\b(?:this|that|the|a|another|the\s+following)\s+"
    r"(?:commit|pull\s+request|pr|mr|changeset|revision|author|contributor|"
    r"developer|maintainer)\b")

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
        tail = clause[m.end(): m.end() + _SUBJECT_WINDOW]
        cut = _RESUBJECT_RE.search(tail)      # stop before a new subject takes over
        if cut:
            tail = tail[:cut.start()]
        if _EDIT_CLAIM_VERB_RE.search(tail):
            return True
    return False


def _claims_file_edits(text: str) -> bool:
    """True when the answer ASSERTS it edited/created a file THIS turn. Evaluated
    PER CLAUSE so a recap/descriptive sentence can't mask a fresh claim in a
    neighbouring one: a clause is a claim when it has an edit verb + a file/code
    object (or "made the changes"), UNLESS it carries an unambiguous prior-work
    cue or a third-party subject drives the edit verb. Conservative — a miss (no
    nudge) is fine; the loop's disk cross-check is the real safety net."""
    if not text:
        return False
    for clause in _SENT_SPLIT.split(text):
        has_claim = ((_EDIT_CLAIM_VERB_RE.search(clause)
                      and _EDIT_CLAIM_OBJ_RE.search(clause))
                     or _MADE_CHANGES_RE.search(clause))
        if not has_claim:
            continue
        if _RECAP_CUE_RE.search(clause) or _subject_is_descriptive(clause):
            continue
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
