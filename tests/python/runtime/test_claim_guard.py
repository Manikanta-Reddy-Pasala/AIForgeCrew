"""Bug1 — claim-vs-reality guard: detect hallucinated file-edit claims."""
from __future__ import annotations

from aiforge_core.runtime.chat_agent._context._claim_guard import (
    _claims_file_edits,
    _edit_claim_disclaimer,
)


def test_claims_file_edits_positive_on_hallucinated_phrasing():
    # The exact shapes from the reported screenshot must all trip the guard.
    assert _claims_file_edits(
        "I have now applied the fix directly to src/views/Dashboard.vue.")
    assert _claims_file_edits("Confirmed Fixes Applied: the filter is fixed.")
    assert _claims_file_edits("I updated the file with the corrected logic.")
    assert _claims_file_edits("Patched app.py and saved the changes.")


def test_claims_file_edits_negative_on_plain_prose():
    # No file/code object → not an edit claim.
    assert not _claims_file_edits("I updated my estimate for the timeline.")
    assert not _claims_file_edits("Here is how the deployment works.")
    # A PROPOSAL is not a past-tense claim of a landed edit.
    assert not _claims_file_edits("You should edit the config to enable it.")
    assert not _claims_file_edits("")


def test_prior_work_recap_is_not_a_claim():
    # A truthful recap with an UNAMBIGUOUS prior-turn cue adjacent to the verb
    # must NOT trip the guard (the false-positive on a "run the tests" turn).
    assert not _claims_file_edits(
        "Previously created isprime.py with is_prime(n). All tests pass.")
    assert not _claims_file_edits(
        "Earlier in this session I updated calc.py with divide().")
    # …but 'already' is NOT a recap cue — it's a this-turn hallucination tell
    # ("I've already applied the fix to X" is the classic fake claim), so it
    # still trips the guard; the loop's disk cross-check (_edits_made>0 / tree
    # changed) is what spares a genuinely-completed edit from a false nudge.
    assert _claims_file_edits("I already applied the fix to Dashboard.vue.")
    # a fresh this-turn claim still trips it.
    assert _claims_file_edits("I have now applied the fix to Dashboard.vue.")


def test_disclaimer_prepends_honest_note_once():
    out = _edit_claim_disclaimer("Applied the fix to app.py.")
    assert out.lower().startswith("⚠ note")
    assert "Applied the fix to app.py." in out


def test_weak_cues_are_not_recaps():
    # 'already' / 'so far' are this-turn hallucination tells, NOT recaps → fire
    assert _claims_file_edits("I've already applied the fix to Dashboard.vue")
    assert _claims_file_edits("So far I saved the changes to config.yaml")
    # unambiguous prior-turn cues → truthful recap, suppress
    assert not _claims_file_edits("Previously created isprime.py in an earlier turn")
    assert not _claims_file_edits("I earlier updated the config file")


def test_descriptive_subject_not_a_claim():
    # a third-party subject DRIVING the edit verb ("commit added", "author
    # removed") = describing someone else's diff → suppress
    assert not _claims_file_edits("This commit added the config and fixed the module")
    assert not _claims_file_edits("The author removed the script")
    # but a descriptive noun used only as an OBJECT ("the diff view component")
    # must NOT suppress a real claim (clause-scoped, subject-adjacency check)
    assert _claims_file_edits("I updated the diff view component in App.vue")
    assert _claims_file_edits("I fixed the config module")
    # 'made the changes' IS a this-turn claim; 'made sure the file exists' is NOT
    assert _claims_file_edits("I made the changes to Dashboard.vue")
    assert not _claims_file_edits("I made sure the file exists before reading it")
    # a recap/descriptive sentence must not mask a fresh claim in a NEXT clause
    assert _claims_file_edits(
        "This commit added logging. I've now applied the fix to config.py")


def test_advisory_prose_is_not_a_claim():
    # modal / passive / imperative SUGGESTIONS are not this-turn edit claims
    assert not _claims_file_edits("the config file should be updated with a 30s timeout")
    assert not _claims_file_edits("A new test file needs to be created")
    assert not _claims_file_edits("The unused import must be removed from utils.py")
    assert not _claims_file_edits("You should update the config module")
    # a third-party TOOL subject driving the verb is descriptive, not a claim
    assert not _claims_file_edits("The linter removed the unused imports from utils.py")
    # real this-turn claims still fire
    assert _claims_file_edits("I updated config.yaml")
    assert _claims_file_edits("Applied the fix to Dashboard.vue")
