from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from aiforge_core.runtime import vision_adk


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    return tmp_path


def test_no_op_when_model_not_vision(repo, monkeypatch):
    img = repo / "x.png"
    img.write_bytes(_PNG_HEADER + b"x" * 100)
    contents = [MagicMock(role="user", parts=[MagicMock()])]
    out = vision_adk.inject_image_parts(contents, "qwen-coder-next", ["x.png"])
    assert out == contents


def test_no_op_when_no_images():
    contents = [MagicMock(role="user", parts=[MagicMock()])]
    out = vision_adk.inject_image_parts(contents, "gpt-4o", [])
    assert out == contents


def test_no_op_when_empty_contents():
    out = vision_adk.inject_image_parts([], "gpt-4o", ["x.png"])
    assert out == []


def test_no_op_when_first_message_not_user():
    contents = [MagicMock(role="system", parts=[MagicMock()])]
    out = vision_adk.inject_image_parts(
        contents, "gpt-4o", ["x.png"],
    )
    assert out == contents


def test_inject_appends_image_part(repo, monkeypatch):
    img = repo / "x.png"
    img.write_bytes(_PNG_HEADER + b"x" * 100)

    # Patch out google.genai.types to avoid heavy ADK dep in test path —
    # use small stubs that record what was passed.
    class _Part:
        def __init__(self, **kw):
            self.kw = kw
        @classmethod
        def from_bytes(cls, *, data, mime_type):
            inst = cls(data=data, mime_type=mime_type)
            return inst
        @classmethod
        def from_text(cls, *, text):
            return cls(text=text)

    class _Content:
        def __init__(self, *, role, parts):
            self.role = role
            self.parts = parts

    fake_types = MagicMock(Part=_Part, Content=_Content)
    import sys
    sys.modules.setdefault("google.genai", MagicMock())
    monkeypatch.setattr(
        "aiforge_core.runtime.vision_adk.gtypes", fake_types, raising=False,
    )
    # vision_adk imports gtypes inside the function — use builtins patch.
    fake_module = MagicMock(types=fake_types)
    monkeypatch.setitem(sys.modules, "google.genai", fake_module)

    initial_part = _Part(text="please look")
    contents = [_Content(role="user", parts=[initial_part])]
    out = vision_adk.inject_image_parts(
        contents, "gpt-4o", ["x.png"],
    )
    assert len(out[0].parts) == 2
    appended = out[0].parts[-1]
    assert getattr(appended, "kw", {}).get("mime_type") == "image/png"


def test_inject_skips_missing_file(repo):
    contents_msg = MagicMock(role="user", parts=[MagicMock()])
    contents = [contents_msg]
    out = vision_adk.inject_image_parts(
        contents, "gpt-4o", ["nope.png"],
    )
    # Original list returned untouched because nothing appended
    assert out is not contents or len(out[0].parts) == 1
