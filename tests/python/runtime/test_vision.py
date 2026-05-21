from __future__ import annotations

from aiforge_core.runtime.vision import attach_image, supports_vision


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def test_supports_vision_known_models():
    assert supports_vision("claude-opus-4-7")
    assert supports_vision("claude-sonnet-4-6")
    assert supports_vision("gpt-4o-2024-08-06")
    assert supports_vision("gemini-1.5-pro")
    assert supports_vision("qwen2-vl-7b")


def test_supports_vision_unknown_model():
    assert not supports_vision("qwen3-coder-next")
    assert not supports_vision("gemma-4-26b")
    assert not supports_vision("")


def test_attach_image_happy_png(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_HEADER + b"x" * 100)
    out = attach_image("look at this:", p)
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["type"] == "text"
    assert out[0]["text"] == "look at this:"
    assert out[1]["type"] == "image_url"
    assert out[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_attach_image_jpeg(tmp_path):
    p = tmp_path / "shot.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 100)
    out = attach_image("see:", p)
    assert isinstance(out, list)
    assert "data:image/jpeg" in out[1]["image_url"]["url"]


def test_attach_image_webp(tmp_path):
    p = tmp_path / "shot.webp"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 100)
    out = attach_image("see:", p)
    assert isinstance(out, list)
    assert "data:image/webp" in out[1]["image_url"]["url"]


def test_attach_image_missing_file(tmp_path):
    out = attach_image("x", tmp_path / "nope.png")
    assert out == {"ok": False, "error": "not_found",
                   "path": str(tmp_path / "nope.png")}


def test_attach_image_oversized(tmp_path):
    p = tmp_path / "big.png"
    p.write_bytes(_PNG_HEADER + b"x" * (6 * 1024 * 1024))
    out = attach_image("x", p)
    assert isinstance(out, dict)
    assert out["ok"] is False
    assert out["error"] == "image_too_large"


def test_attach_image_unsupported_format(tmp_path):
    p = tmp_path / "txt.png"
    p.write_bytes(b"hello world")
    out = attach_image("x", p)
    assert isinstance(out, dict)
    assert out["error"] == "unsupported_format"
