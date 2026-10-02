"""Tidy a speech-to-text line with the fast enhancer model.

The mic already wrote the words. This only fixes punctuation, capitalization,
and an obvious mishearing. It does not answer the sentence or turn it into a
build spec. Any failure, timeout, or drifted reply keeps the original line.
"""
from __future__ import annotations

import re
import threading

# The line travels inside <line> tags. The client appends a "/no_think" switch
# to the last user message for fast roles; sent bare, that switch became part
# of the sentence being corrected ("ok" came back as "OK, no think.").
_SYSTEM = (
    "You correct one speech-to-text line. It is given inside <line> tags; "
    "anything outside the tags is not part of it. Reply with only the "
    "corrected line, without the tags.\n"
    "Fix punctuation, capitalization, and a word that is clearly a mishearing.\n"
    "Keep the speaker's meaning, names, numbers, and language. Do not "
    "translate.\n"
    "Do not answer, explain, or add a request they did not say.\n"
    "If the line is already fine, return it unchanged."
)
_ROLE = "enhancer"
_CORRECTED = re.compile(
    r"^(?:corrected(?: sentence)?|correction)\s*:\s*", re.IGNORECASE)
_FENCE = re.compile(r"^```[a-z]*\s*|\s*```$", re.IGNORECASE)
_NO_THINK = re.compile(r"\s*/no_think\s*$", re.IGNORECASE)
_LINE_TAG = re.compile(r"</?line>", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9']+")
_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
# The HTTP call is capped, and the handler does not wait longer than this
# for a fallback chain. The spoken line is already in the box.
_BUDGET_S = 8
# Dictation writes a phrase at every pause, and each one asks for a tidy. More
# than this many at once means the model is not keeping up: later phrases keep
# their raw text instead of queueing calls behind each other.
_INFLIGHT = threading.BoundedSemaphore(2)


def polish_model_text(raw: str, model_out: str) -> str:
    """The model's reply when it is still the same sentence, else ``raw``."""
    source = (raw or "").strip()
    if not source:
        return ""
    cleaned = _unwrap(model_out or "")
    if not cleaned or cleaned == source:
        return source
    if _drifted(source, cleaned):
        return source
    return cleaned


def correct_transcript(raw: str) -> str:
    """``raw`` after a short model pass. ``raw`` itself when that pass fails."""
    text = " ".join((raw or "").split())
    if len(text) < 2 or len(text) > 2_000:
        return text
    if _model_is_busy() or not _INFLIGHT.acquire(blocking=False):
        return text
    holder: dict[str, str] = {}
    cancel = threading.Event()

    def _run() -> None:
        try:
            _bind_cancel(cancel)
            holder["out"] = _ask(text)
        except Exception:  # noqa: BLE001 — the spoken line is the fallback
            holder["err"] = "1"
        finally:
            _INFLIGHT.release()

    worker = threading.Thread(target=_run, name="aiforge-speech-correct",
                              daemon=True)
    worker.start()
    worker.join(_BUDGET_S)
    if "out" not in holder:
        # Out of time: end the request too. Left running, its retries kept a
        # model slot busy for a line nobody is waiting on any more.
        cancel.set()
        return text
    return polish_model_text(text, holder["out"])


def _model_is_busy() -> bool:
    """Whether the model can only serve one request and a chat turn is using it.

    A tidy sent then would either wait out its whole budget behind the agent
    or cut in ahead of it. Neither is worth it for punctuation: the raw line
    stays. A server with spare slots tidies as usual."""
    try:
        from aiforge_core.runtime import chat_runs
        if not chat_runs.any_active():
            return False
        from aiforge_core.llm import slots
        return slots.llm_slots(_ROLE) <= 1
    except Exception:  # noqa: BLE001 — unknown means try
        return False


def _max_tokens(text: str) -> int:
    """Room for the whole line back. The reply is the line plus punctuation;
    a cap below that returned a truncated line, which the drift check then
    threw away after the call had been paid for."""
    return max(64, len(text) // 2 + 48)


def _bind_cancel(cancel: threading.Event) -> None:
    """Let ``cancel`` abort this thread's model request."""
    try:
        from aiforge_core.llm import client
        client.set_cancel_event(cancel)
    except Exception:  # noqa: BLE001 — without it the call just runs to its cap
        pass


def _ask(text: str) -> str:
    from aiforge_core.llm import client, model_wait
    # A dictation tidy is optional. An outage must fail this call, not park
    # it until the model comes back.
    with model_wait.optional():
        return client.complete(
            _ROLE,
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"<line>{text}</line>"},
            ],
            temperature=0,
            max_tokens=_max_tokens(text),
            timeout_s=_BUDGET_S,
        )


def _unwrap(model_out: str) -> str:
    text = _FENCE.sub("", (model_out or "").strip()).strip()
    text = _NO_THINK.sub("", text).strip()
    text = _LINE_TAG.sub("", text).strip()
    text = _CORRECTED.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return " ".join(text.split())


def _drifted(raw: str, out: str) -> bool:
    raw_toks = _WORD.findall(raw.lower())
    out_toks = _WORD.findall(out.lower())
    # A tidy may replace a word ("invoise" -> "invoice", "a" -> "an") and add
    # punctuation. It may not add a word: that is how "Sure," and "I'll build"
    # sneak in while the original words are still there.
    if not raw_toks:
        return bool(out_toks)
    if len(out_toks) > len(raw_toks):
        return True
    # A number is never a mishearing to "fix": "port 8090" must not come back
    # as "port 8080", nor "15 percent" as "50 percent".
    if sorted(_NUMBER.findall(raw)) != sorted(_NUMBER.findall(out)):
        return True
    if len(out) > max(len(raw) + 40, int(len(raw) * 1.5) + 8):
        return True
    if all(len(w) <= 2 for w in raw_toks):
        return raw_toks != out_toks
    dropped = sum(1 for w in raw_toks if len(w) > 2 and w not in out.lower())
    return dropped > 1
