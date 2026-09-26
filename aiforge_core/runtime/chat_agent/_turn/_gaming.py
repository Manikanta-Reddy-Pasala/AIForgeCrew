"""The test-gaming check on a chat FINAL (see
:mod:`aiforge_core.runtime.gaming_check`).

Runs when an act-mode turn that edited files is about to finish with the
tests green. On a hit: one directed nudge back to the model ("this passes
the tests by detecting the test, not by fixing behaviour; if the tests
contradict each other, say so and ask"). If the next FINAL still games the
tests, the answer goes out with an explicit warning in front of it — never
silently accepted.
"""
from __future__ import annotations

import logging

log = logging.getLogger("aiforge.chat.test_gaming")

_MAX_NUDGES = 1


def note_test_result(st, ok) -> None:
    """A test run (or the project verify) finished: remember whether the
    tests are green now."""
    if ok is True:
        st.tests_green = True
    elif ok is False:
        st.tests_green = False


def gate(st, step, cwd, plan_mode, builder):
    """Generator: ``"continue"`` after a nudge, else None (the answer — with
    a warning prepended when the gaming persisted — is accepted)."""
    if (plan_mode or builder or getattr(st, "edits_made", 0) <= 0
            or not getattr(st, "tests_green", False)):
        return None
    try:
        from aiforge_core.runtime import gaming_check as test_gaming
        evidence = test_gaming.check(str(cwd or ""))
    except Exception:  # noqa: BLE001 — the check never breaks a turn
        log.debug("test-gaming check failed", exc_info=True)
        return None
    if not evidence:
        return None
    log.warning("test gaming on FINAL: %s", "; ".join(evidence))
    nudges = int(getattr(st, "gaming_nudges", 0) or 0)
    if nudges < _MAX_NUDGES:
        st.gaming_nudges = nudges + 1
        if step.get("text"):          # the streamed answer, set aside
            yield {"type": "thought", "text": step["text"]}
        yield {"type": "thought", "role": "system",
               "text": "⚠ the tests pass by detecting the test, not by a "
                       "fix — sending it back once…"}
        st.convo.append({"role": "user",
                         "content": test_gaming.nudge_text(evidence)})
        return "continue"
    step["text"] = test_gaming.warning_text(evidence) + (step.get("text") or "")
    st.quality_issue = {"kind": "test_gaming", "evidence": evidence}
    yield {"type": "thought", "role": "system",
           "text": "⚠ the tests still pass only by detecting the test — "
                   "finishing with a warning."}
    return None


__all__ = ["gate", "note_test_result"]
