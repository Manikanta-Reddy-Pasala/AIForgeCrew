"""POST /api/chat/speech-correct — tidy one dictated line."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ._core import router


class _SpeechCorrectBody(BaseModel):
    text: str = Field("", max_length=4_000)


@router.post("/api/chat/speech-correct")
def chat_speech_correct(body: _SpeechCorrectBody) -> dict:
    """Fix punctuation and an obvious mishearing. The raw line comes back
    when the model is unavailable or wanders off the sentence."""
    from aiforge_core.runtime.speech_correct import correct_transcript
    return {"text": correct_transcript(body.text)}
