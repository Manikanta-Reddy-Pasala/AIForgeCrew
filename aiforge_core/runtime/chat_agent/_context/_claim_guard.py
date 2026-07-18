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
_EDIT_CLAIM_VERB_RE = re.compile(
    r"(?i)\b(?:applied|committed|wrote|written|saved|updated|modified|edited|"
    r"patched|replaced|inserted|refactored|implemented|created|corrected|"
    r"adjusted|rewrote|overwrote)\b")
# An OBJECT that makes the verb about a file/code artifact — a path with an
# extension, an explicit file/code noun, or the screenshot's "fixes applied"
# heading. Required ALONGSIDE the verb so prose like "I updated my estimate"
# doesn't trip it.
_EDIT_CLAIM_OBJ_RE = re.compile(
    r"(?i)(?:\bthe\s+(?:file|code|function|method|component|class|config|"
    r"module|script)\b|\bfiles?\b|\bfix(?:es)?\s+applied\b|\bchanges?\s+"
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

def _claims_file_edits(text: str) -> bool:
    """True when the answer ASSERTS it edited/created a file THIS turn. Requires
    an edit verb AND a file/code object so plain prose doesn't false-fire, and is
    suppressed only by an UNAMBIGUOUS prior-work cue (previously / earlier / in a
    prior turn — NOT "already" / "so far", which are this-turn hallucination
    tells). Conservative — a miss (no nudge) is fine; the loop's disk cross-check
    is the real safety net."""
    if not text:
        return False
    if _RECAP_CUE_RE.search(text):
        return False
    return bool(_EDIT_CLAIM_VERB_RE.search(text)
                and _EDIT_CLAIM_OBJ_RE.search(text))


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
