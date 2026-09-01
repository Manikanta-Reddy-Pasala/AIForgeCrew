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


# ── the adapter's own fail-open paths ────────────────────────────────────
#
# Every one of these executes only when something is ALREADY wrong. They are
# the difference between "the structured path degraded" and "the turn died",
# and none of them had a test.


def test_availability_is_a_question_not_an_exception(monkeypatch):
    """Callers branch on this to choose the fallback; it may never raise."""
    import sys

    from aiforge_core.integrations import instructor_adapter as ia
    monkeypatch.setitem(sys.modules, "instructor", None)
    assert ia.available() is False


def test_a_junk_int_env_falls_back(monkeypatch):
    from aiforge_core.integrations import instructor_adapter as ia
    monkeypatch.setenv("AIFORGE_STRUCTURED_SDK_RETRIES", "lots")
    assert ia._int_env("AIFORGE_STRUCTURED_SDK_RETRIES", 0) == 0
    monkeypatch.setenv("AIFORGE_STRUCTURED_SDK_RETRIES", "2")
    assert ia._int_env("AIFORGE_STRUCTURED_SDK_RETRIES", 0) == 2


def test_a_junk_wait_budget_falls_back(monkeypatch):
    from aiforge_core.integrations import instructor_adapter as ia
    monkeypatch.setenv("AIFORGE_LLM_MAX_WAIT_S", "forever")
    assert ia._wait_budget() == 120.0


def test_an_unreadable_error_body_is_empty_not_a_crash():
    """httpx fires response hooks BEFORE the body is read, so `.text` raises
    ResponseNotRead on a real streamed response — the one case the mock
    transport used in tests does NOT reproduce."""
    from aiforge_core.integrations import instructor_adapter as ia

    class _Unreadable:
        def read(self):
            raise RuntimeError("not read")

        @property
        def text(self):
            raise RuntimeError("still not read")

    assert ia._read_error_body(_Unreadable()) == ""


def test_settling_nothing_is_not_an_error():
    from aiforge_core.integrations import instructor_adapter as ia
    ia._settle_pending([], "no_response")     # must not raise


def test_a_broken_meter_cannot_break_a_settle(monkeypatch):
    from aiforge_core.integrations import instructor_adapter as ia
    from aiforge_core.llm import call_meter

    monkeypatch.setattr(call_meter, "record_failure",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    ia._settle_pending(["tok"], "boom")       # must not raise


def test_the_unmetered_charge_still_goes_through_the_gateway(monkeypatch):
    """When the metered client cannot be built there are no per-send hooks, so
    ONE charge is booked up front. Undercounting a retry is a smaller error than
    exempting the whole path."""
    from aiforge_core.integrations import instructor_adapter as ia
    from aiforge_core.llm import rate_limiter as rl

    rl.reset_global()
    before = rl.global_used()
    ia._charge_one_unmetered("learner", "m", "openai_compatible")
    assert rl.global_used() == before + 1
    rl.reset_global()


def test_a_limiter_fault_does_not_stop_the_unmetered_charge(monkeypatch):
    from aiforge_core.integrations import instructor_adapter as ia
    from aiforge_core.llm import rate_limiter as rl

    monkeypatch.setattr(rl, "govern_send",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("down")))
    ia._charge_one_unmetered("learner", "m", "openai_compatible")   # no raise


def test_a_max_tokens_floor_protects_short_extractions(monkeypatch):
    """A truncated JSON reply raises IncompleteOutputException and forces the
    fallback loop — a wasted call for the sake of a small number."""
    from aiforge_core.integrations import instructor_adapter as ia
    monkeypatch.delenv("AIFORGE_STRUCTURED_MAX_TOKENS", raising=False)
    assert ia._structured_max_tokens(16) >= 4096
    assert ia._structured_max_tokens(None) >= 4096
    assert ia._structured_max_tokens(99999) == 99999
