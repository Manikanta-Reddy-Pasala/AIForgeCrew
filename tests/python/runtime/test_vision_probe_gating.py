"""A text-only chat turn must NOT trigger a vision probe (a live LLM call).
Probing every turn regardless of attachments made a down/slow endpoint block
chat setup for the probe timeout x retries — the root cause of the
test_chat_ask_policy hang and of per-message latency in production."""
from __future__ import annotations

from aiforge_core.runtime import chat_media


def test_no_images_skips_vision_probe(monkeypatch):
    # No media rows → no probe, no network, empty blocks.
    monkeypatch.setattr("aiforge_core.runtime.chat_store.list_media",
                        lambda sid: [])

    def _boom(*a, **k):
        raise AssertionError("vision_enabled/probe must NOT run for a text-only turn")

    monkeypatch.setattr(chat_media, "vision_enabled", _boom)
    assert chat_media.image_blocks_for_turn(1, "chat") == []


def test_images_present_does_probe(monkeypatch):
    monkeypatch.setattr("aiforge_core.runtime.chat_store.list_media",
                        lambda sid: [{"mime": "image/png", "filename": "a.png",
                                      "path": "/tmp/a.png"}])
    seen = {"probed": False}

    def _fake_enabled(role, *, probe=False):
        seen["probed"] = probe
        return False   # not vision-capable → empty blocks, but probe DID run

    monkeypatch.setattr(chat_media, "vision_enabled", _fake_enabled)
    assert chat_media.image_blocks_for_turn(1, "chat") == []
    assert seen["probed"] is True
