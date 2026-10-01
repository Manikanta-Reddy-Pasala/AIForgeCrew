"""Tidy a speech-to-text line with the fast enhancer model.

The mic already wrote the words. This only fixes punctuation, capitalization,
and an obvious mishearing. It does not answer the sentence or turn it into a
build spec. Any failure, timeout, or drifted reply keeps the original line.
"""
from __future__ import annotations

import re
import threading

_SYSTEM = (
    "You correct one speech-to-text line. Reply with only the corrected line.\n"
    "Fix punctuation, capitalization, and a word that is clearly a mishearing.\n"
    "Keep the speaker's meaning, names, and language. Do not translate.\n"
    "Do not answer, explain, or add a request they did not say.\n"
    "If the line is already fine, return it unchanged."
)
_CORRECTED = re.compile(
    r"^(?:corrected(?: sentence)?|correction)\s*:\s*", re.IGNORECASE)
_FENCE = re.compile(r"^```[a-z]*\s*|\s*```$", re.IGNORECASE)
_NO_THINK = re.compile(r"\s*/no_think\s*$", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9']+")
# The HTTP call is capped, and the handler does not wait longer than this
# for a fallback chain. The spoken line is already in the box.
_BUDGET_S = 8


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
    holder: dict[str, str] = {}

    def _run() -> None:
        try:
            holder["out"] = _ask(text)
        except Exception:  # noqa: BLE001 — the spoken line is the fallback
            holder["err"] = "1"

    worker = threading.Thread(target=_run, name="aiforge-speech-correct",
                              daemon=True)
    worker.start()
    worker.join(_BUDGET_S)
    if "out" not in holder:
        return text
    return polish_model_text(text, holder["out"])


def _ask(text: str) -> str:
    from aiforge_core.llm import client, model_wait
    # A dictation tidy is optional. An outage must fail this call, not park
    # it until the model comes back.
    with model_wait.optional():
        return client.complete(
            "enhancer",
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=min(400, max(48, len(text) // 3 + 32)),
            timeout_s=_BUDGET_S,
        )


def _unwrap(model_out: str) -> str:
    text = _FENCE.sub("", (model_out or "").strip()).strip()
    text = _NO_THINK.sub("", text).strip()
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
    if len(out) > max(len(raw) + 40, int(len(raw) * 1.5) + 8):
        return True
    if all(len(w) <= 2 for w in raw_toks):
        return raw_toks != out_toks
    dropped = sum(1 for w in raw_toks if len(w) > 2 and w not in out.lower())
    return dropped > 1
