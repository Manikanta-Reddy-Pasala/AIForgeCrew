"""One capped call: what does this user probably want next?

Modelled on ``rule_capture._classify`` deliberately — same capped single call on
a cheap role, same confidence floor, same fail-open on every path. That module
is this codebase's precedent for "an always-on LLM pass that must never break a
turn", and copying its shape is cheaper than rediscovering its lessons.

The prompt carries three things: what the user said, what the agent actually did
about it, and the last few predictions this user ACCEPTED in this repo. The
third is the entire learning mechanism — no training and no embeddings, just
showing the model what this person has said yes to before.
"""
from __future__ import annotations

import logging
import os
import re
import uuid

log = logging.getLogger("aiforge.next_step")

_SYS = (
    "You predict the ONE next action a developer most likely wants, given what "
    "they just asked and what was just done for them. Reply with ONLY a JSON "
    'object: {"action":"<one imperative sentence>","tool":"<tool name or '
    'empty>","args":{...},"confidence":0.0-1.0,"rationale":"<one clause>"}. '
    "Be conservative: if the next step is not strongly implied by what just "
    "happened, return a confidence below 0.5. Never propose an action that "
    "deletes data, pushes code, deploys, or spends money.\n"
    "MOST REQUESTS NEED NOTHING NEXT. If the request was already answered, say "
    'so with {"action":"","confidence":0.0} — that is the expected reply, not '
    "a failure. Never restate, rephrase, confirm or summarise what was just "
    "asked or just done: repeating the request back is not a next step. A real "
    "next step names the tool it would use; if you cannot name one, your "
    "confidence must be below 0.5."
)

_MAX_EXAMPLES = 5
_DEFAULT_TIMEOUT_S = 10

# Words that carry no topic and so must not count as agreement between two
# sentences. Deliberately short: this list only has to stop the commonest
# connectives from inflating an overlap score.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "what", "which", "who", "how",
    "why", "when", "where", "you", "your", "our", "are", "was", "were", "has",
    "have", "had", "can", "could", "would", "should", "will", "just", "one",
    "two", "please", "then", "than", "from", "into", "out", "off", "its",
    "it's", "does", "did", "doing", "done", "not", "any", "all", "some",
    "short", "sentence", "single", "exactly", "reply", "answer",
})

# Above this share of an action's content words appearing in the text it
# followed, the "next step" is a rewording of what already happened. 0.7 was
# picked against the three echoes actually observed in production (the worst
# scored 0.8) while leaving a genuine follow-up like "run the tests for
# chat_pipeline" after "fix the run lock in chat_pipeline" (0.67) alone.
_RESTATEMENT_RATIO = 0.7
_MIN_WORDS_TO_JUDGE = 3


def _content_words(text: str) -> set[str]:
    """Topic-bearing words of ``text``, crudely singularised.

    Correct linguistics is not the goal and would not help: both sides are
    normalised the same way, so a consistent mangling compares exactly as well
    as a right one.
    """
    out: set[str] = set()
    for raw in re.findall(r"[a-z0-9_./-]+", str(text or "").lower()):
        word = raw.strip("./-")
        if len(word) < 3 or word in _STOPWORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return out


def _echoes(action: str, prior: str) -> bool:
    """True when ``action`` is mostly a rewording of ``prior``."""
    a = _content_words(action)
    if len(a) < _MIN_WORDS_TO_JUDGE:
        # Too little to judge, and guessing here would silently drop terse but
        # perfectly good suggestions.
        return False
    p = _content_words(prior)
    if not p:
        return False
    return len(a & p) / len(a) >= _RESTATEMENT_RATIO


def is_restatement(action: str, ctx: dict) -> bool:
    """True when the "next step" merely restates the turn that just ended.

    The failure this exists for: asked for the next action after a request that
    is already fully answered, a model reliably rephrases that request back —
    observed on every one of the first three live predictions, each at
    confidence 0.95. The chip then auto-sent it and the user's own question ran
    a second time. Confidence cannot catch this; the model is not wrong about
    what was wanted, only about it still being wanted.
    """
    text = str(action or "")
    if not text.strip():
        return False
    c = ctx or {}
    return _echoes(text, str(c.get("message") or "")) or \
        _echoes(text, str(c.get("did") or ""))


def _disabled() -> bool:
    return (os.environ.get("AIFORGE_PREDICT_DISABLE") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _timeout() -> int:
    try:
        return int(os.environ.get("AIFORGE_PREDICT_TIMEOUT_S") or _DEFAULT_TIMEOUT_S)
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def _llm(role: str, messages: list, **kw) -> str:
    """Indirection so a test can replace the call without touching the network.

    Delegates to ``rule_capture``'s helper rather than building a second client:
    that one already knows how this codebase reaches a model role.
    """
    from aiforge_core.runtime.rule_capture import _llm_complete

    return _llm_complete(role, messages, **kw)


def _examples(repo: str) -> str:
    from aiforge_core.runtime.next_step import _store

    try:
        rows = _store.accepted(repo, limit=_MAX_EXAMPLES)
    except Exception:  # noqa: BLE001 — no history is not a reason to predict nothing
        return ""
    if not rows:
        return ""
    lines = "\n".join(f"- after {r.get('trigger')}: {r.get('action')}" for r in rows)
    return f"\n\nThis user previously accepted:\n{lines}"


def _dismissed(repo: str) -> str:
    """What this user has said NO to, verbatim, as a do-not-repeat list.

    Dismissals were recorded from the first version and fed back into nothing,
    so the single clearest signal in the store — *this suggestion is unwanted* —
    never reached the thing writing suggestions. That is why the same two or
    three came back in every new chat: each one started from an empty room.
    """
    from aiforge_core.runtime.next_step import _store

    try:
        rows = _store.dismissed(repo, limit=_MAX_EXAMPLES)
    except Exception:  # noqa: BLE001 — no history is not a reason to say nothing
        return ""
    if not rows:
        return ""
    lines = "\n".join(f"- {r.get('action')}" for r in rows)
    return ("\n\nThis user DISMISSED these — do not propose them again, or "
            f"anything that only rewords them:\n{lines}")


def _user_prompt(ctx: dict) -> str:
    repo = str(ctx.get("repo") or "")
    return (f"They said: {str(ctx.get('message') or '')[:2000]}\n"
            f"What was just done: {str(ctx.get('did') or '')[:1000]}"
            f"{_examples(repo)}{_dismissed(repo)}")


def _parse(raw: str) -> dict | None:
    """The model's reply as a normalised dict, or None. Never raises."""
    from aiforge_core.runtime.rule_capture import _classify

    try:
        obj = _classify._extract_json(raw or "")
    except Exception:  # noqa: BLE001 — unparseable is simply no prediction
        return None
    if not isinstance(obj, dict):
        return None
    action = str(obj.get("action") or "").strip().replace("\n", " ")
    if not action:
        return None
    try:
        confidence = float(obj.get("confidence"))
    except (TypeError, ValueError):
        # A prediction with no confidence cannot be gated, and an ungated
        # prediction is the one thing this design does not allow.
        return None
    args = obj.get("args")
    return {"id": f"p-{uuid.uuid4().hex[:8]}",
            "action": action[:300],
            "tool": str(obj.get("tool") or "").strip(),
            "args": args if isinstance(args, dict) else {},
            "confidence": confidence,
            "rationale": str(obj.get("rationale") or "").strip()[:300]}


def raw_prediction(ctx: dict) -> dict | None:
    """The model's answer, normalised, or None. Never raises."""
    if _disabled():
        return None
    role = os.environ.get("AIFORGE_PREDICT_ROLE", "enhancer")
    try:
        raw = _llm(role,
                   [{"role": "system", "content": _SYS},
                    {"role": "user", "content": _user_prompt(ctx or {})}],
                   max_tokens=250, temperature=0.0, timeout_s=_timeout())
    except Exception as exc:  # noqa: BLE001 — a prediction never breaks a turn
        log.debug("next_step: prediction call failed (none): %s", exc)
        return None
    return _parse(raw or "")


__all__ = ["raw_prediction"]
