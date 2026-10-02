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


def test_a_changed_number_is_rejected():
    assert sc.polish_model_text("open port 8090", "Open port 8080.") == "open port 8090"
    assert sc.polish_model_text("cut it by 15 percent", "Cut it by 50 percent.") == (
        "cut it by 15 percent")
    # punctuation around the same number is still a tidy
    assert sc.polish_model_text("open port 8090 now", "Open port 8090 now.") == (
        "Open port 8090 now.")


def test_the_reply_has_room_for_the_whole_line():
    long_line = "word " * 380          # ~1900 chars; the old cap was 400 tokens
    assert sc._max_tokens(long_line) > len(long_line) // 3
    assert sc._max_tokens("hi") == 64


def test_a_busy_single_slot_model_is_not_asked(monkeypatch):
    from aiforge_core.llm import slots
    from aiforge_core.runtime import chat_runs
    monkeypatch.setattr(sc, "_ask", lambda text: (_ for _ in ()).throw(
        AssertionError("the model is busy with a chat turn")))
    monkeypatch.setattr(chat_runs, "any_active", lambda: True)
    monkeypatch.setattr(slots, "llm_slots", lambda role="chat": 1)
    assert sc.correct_transcript("save the file") == "save the file"
    # spare slots: the tidy runs beside the turn
    monkeypatch.setattr(slots, "llm_slots", lambda role="chat": 4)
    monkeypatch.setattr(sc, "_ask", lambda text: "Save the file.")
    assert sc.correct_transcript("save the file") == "Save the file."


def test_a_pile_up_keeps_the_raw_line_instead_of_queueing(monkeypatch):
    import threading
    gate = threading.Event()
    started = threading.Semaphore(0)

    def _slow(text):
        started.release()
        gate.wait(5)
        return "Save the file."

    monkeypatch.setattr(sc, "_ask", _slow)
    monkeypatch.setattr(sc, "_BUDGET_S", 0.05)
    assert sc.correct_transcript("save the file") == "save the file"   # timed out
    assert sc.correct_transcript("save the file") == "save the file"
    started.acquire(timeout=2)
    started.acquire(timeout=2)
    # both slots are still held by the slow calls: a third is not even sent
    monkeypatch.setattr(sc, "_ask", lambda text: (_ for _ in ()).throw(
        AssertionError("should not queue a third call")))
    assert sc.correct_transcript("save the file") == "save the file"
    gate.set()


def test_running_out_of_time_cancels_the_request(monkeypatch):
    import threading
    seen = {}
    release = threading.Event()

    def _bind(cancel):
        seen["cancel"] = cancel

    def _slow(text):
        release.wait(5)
        return "Save the file."

    monkeypatch.setattr(sc, "_bind_cancel", _bind)
    monkeypatch.setattr(sc, "_ask", _slow)
    monkeypatch.setattr(sc, "_BUDGET_S", 0.05)
    assert sc.correct_transcript("save the file") == "save the file"
    assert seen["cancel"].is_set()
    release.set()


def test_the_line_is_framed_so_a_client_suffix_is_not_part_of_it(monkeypatch):
    """The client adds "/no_think" to the last user message for fast roles."""
    from aiforge_core.llm import client
    sent = {}

    def _complete(role, messages, **kw):
        sent["role"], sent["messages"], sent["kw"] = role, messages, kw
        return "<line>Save the file.</line>"

    monkeypatch.setattr(client, "complete", _complete)
    out = sc._ask("save the file")
    assert sent["messages"][-1]["content"] == "<line>save the file</line>"
    assert "inside <line> tags" in sent["messages"][0]["content"]
    assert sent["role"] == "enhancer" and sent["kw"]["timeout_s"] == sc._BUDGET_S
    # an echoed tag is not kept
    assert sc.polish_model_text("save the file", out) == "Save the file."
