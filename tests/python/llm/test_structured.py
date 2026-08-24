"""structured_complete — schema-prompt + extract + validate + reask fallback.
The instructor adapter path is exercised only when the optional dep is
installed; the fallback loop (over client.complete, keeping escalation) is
the contract these tests pin."""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from aiforge_core.llm import structured


class _Plan(BaseModel):
    files: list[dict] = []


class _Point(BaseModel):
    x: int
    y: int


def test_extract_json_variants():
    assert structured.extract_json('{"a": 1}') == '{"a": 1}'
    assert structured.extract_json('prose {"a": 1} trailing') == '{"a": 1}'
    assert structured.extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert structured.extract_json("[1, 2]") == "[1, 2]"
    assert structured.extract_json("no json here") is None
    assert structured.extract_json("") is None
    # stray CLOSING fence after bare JSON must not clobber the payload
    # (empty-string `in "{["` was True → returned None)
    assert structured.extract_json('{"a": 1}\n```') == '{"a": 1}'
    # prose fence first, JSON fence second — scan ALL blocks
    assert structured.extract_json(
        '```\nnotes\n```\n```json\n{"b": 2}\n```') == '{"b": 2}'


def test_fallback_validates_first_try(monkeypatch):
    monkeypatch.setenv("AIFORGE_STRUCTURED_MODE", "fallback")
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda role, msgs, **k: 'sure! {"x": 1, "y": 2}')
    out = structured.structured_complete("architect", [
        {"role": "user", "content": "point"}], _Point)
    assert (out.x, out.y) == (1, 2)


def test_fallback_reasks_on_invalid_then_succeeds(monkeypatch):
    monkeypatch.setenv("AIFORGE_STRUCTURED_MODE", "fallback")
    replies = iter(['{"x": "not-an-int"}', '{"x": 3, "y": 4}'])
    seen: list[list[dict]] = []

    def fake(role, msgs, **k):
        seen.append(list(msgs))
        return next(replies)

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake)
    out = structured.structured_complete("architect", [
        {"role": "user", "content": "point"}], _Point, max_retries=2)
    assert (out.x, out.y) == (3, 4)
    # the reask carried the validation error back to the model
    assert any("did not validate" in (m.get("content") or "")
               for m in seen[-1])


def test_fallback_raises_after_exhaustion(monkeypatch):
    monkeypatch.setenv("AIFORGE_STRUCTURED_MODE", "fallback")
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda role, msgs, **k: "not json at all")
    with pytest.raises(ValueError, match="structured output failed"):
        structured.structured_complete("architect", [
            {"role": "user", "content": "point"}], _Point, max_retries=1)


def test_schema_lands_in_system_prompt(monkeypatch):
    monkeypatch.setenv("AIFORGE_STRUCTURED_MODE", "fallback")
    seen = {}

    def fake(role, msgs, **k):
        seen["sys"] = msgs[0]["content"]
        return '{"x": 1, "y": 2}'

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake)
    structured.structured_complete("architect", [
        {"role": "system", "content": "base"},
        {"role": "user", "content": "point"}], _Point)
    assert seen["sys"].startswith("base")
    assert "JSON Schema" in seen["sys"]
    assert '"x"' in seen["sys"]


def test_architect_uses_structured_path(monkeypatch, tmp_path):
    """The architect seam returns validated file specs through the fallback
    loop (client.complete monkeypatched = no instructor, no network)."""
    monkeypatch.setenv("AIFORGE_STRUCTURED_MODE", "fallback")
    monkeypatch.setattr(
        "aiforge_core.llm.client.complete",
        lambda role, msgs, **k:
        '{"files": [{"path": "db.py", "purpose": "store"},'
        ' {"path": "", "purpose": "dropped"}]}')
    from aiforge_core.runtime import parallel_subtasks as ps
    files = ps._architect("build a store", cwd=str(tmp_path))
    assert files == [{"path": "db.py", "purpose": "store", "api": []}]
