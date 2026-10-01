"""Dictation tidy: keep the sentence, drop a reply that wandered off."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import chat as chat_routes
from aiforge_core.runtime import speech_correct as sc


def test_a_punctuation_fix_is_kept():
    raw = "i want to add a invoice button"
    assert sc.polish_model_text(raw, "I want to add an invoice button.") == (
        "I want to add an invoice button.")


def test_quotes_fences_and_a_label_are_stripped():
    raw = "save the file"
    wrapped = "```\nCorrected: \"Save the file.\"\n```"
    assert sc.polish_model_text(raw, wrapped) == "Save the file."


def test_a_no_think_echo_is_dropped():
    assert sc.polish_model_text("save it", "Save it. /no_think") == "Save it."


def test_an_answer_instead_of_the_sentence_is_rejected():
    raw = "add the invoice button"
    essay = ("Sure, I can help you build an invoice button. First open the "
             "composer component and add a handler, then write a test.")
    assert sc.polish_model_text(raw, essay) == raw
    # Same words, then a short second sentence. Still not a tidy.
    extra = "Add the invoice button. I'll build."
    assert sc.polish_model_text(raw, extra) == raw
    assert sc.polish_model_text("save the file", "Sure, save the file.") == (
        "save the file")


def test_a_short_line_is_not_replaced_by_an_offer_of_help():
    assert sc.polish_model_text("do it", "Sure, I can help with that.") == "do it"
    assert sc.polish_model_text("do it", "Do it.") == "Do it."


def test_an_empty_model_reply_keeps_the_line():
    assert sc.polish_model_text("save it", "   ") == "save it"


def test_correct_transcript_uses_the_model_and_falls_back(monkeypatch):
    seen = {}

    def _ask(text):
        seen["text"] = text
        return "Save the file."

    monkeypatch.setattr(sc, "_ask", _ask)
    assert sc.correct_transcript("save the file") == "Save the file."
    assert seen["text"] == "save the file"

    def _boom(text):
        raise RuntimeError("model down")

    monkeypatch.setattr(sc, "_ask", _boom)
    assert sc.correct_transcript("save the file") == "save the file"


def test_a_blank_line_skips_the_model(monkeypatch):
    def _ask(text):
        raise AssertionError("model should not be called")

    monkeypatch.setattr(sc, "_ask", _ask)
    assert sc.correct_transcript("  ") == ""
    assert sc.correct_transcript("a") == "a"


def test_the_route_returns_the_tidied_line(monkeypatch):
    monkeypatch.setattr(sc, "correct_transcript", lambda text: text.upper())
    app = FastAPI()
    app.include_router(chat_routes.router)
    res = TestClient(app).post("/api/chat/speech-correct", json={"text": "save it"})
    assert res.status_code == 200
    assert res.json() == {"text": "SAVE IT"}
