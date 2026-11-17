"""A question the user already answered during the turn is not asked again."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _ask_wait as A

Q = ("I need one clarification. 1. In-place recolour 2. Light restyle. "
     "Which do you want — 1 (pure recolour) or 2 (light restyle)?")


def _run(gen):
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def test_choice_detection():
    assert A.asks_a_choice(Q)
    assert A.asks_a_choice("Done. Should I also update the README?")
    assert not A.asks_a_choice("Done: both versions are in decks/v6a.pptx and decks/v6b.pptx.")


def test_answered_question_is_bounced_with_the_users_words():
    st = SimpleNamespace(convo=[], steers=["try both versions", "generate both versions"])
    assert _run(A.answered_gate(st, {"text": Q})) == "continue"
    note = st.convo[-1]["content"]
    assert "already answered" in note and "generate both versions" in note and "each one" in note
    # nothing new from the user since: the next question is not bounced again
    assert _run(A.answered_gate(st, {"text": Q})) is None


def test_no_answer_no_bounce_and_limit():
    st = SimpleNamespace(convo=[], steers=[])
    assert _run(A.answered_gate(st, {"text": Q})) is None
    st = SimpleNamespace(convo=[], steers=["a"])
    for i in range(3):
        st.steers.append(f"more {i}")
        A.bounce_if_answered(st, Q)
    assert st.answered_bounces == 2
