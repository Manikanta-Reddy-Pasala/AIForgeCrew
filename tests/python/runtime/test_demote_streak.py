"""Fix 4 — one transient blip must NOT sticky-demote the local primary for
the whole run. With _attempt_retries=1 (fast escalation), a single primary
failure used to set primary_demoted=True immediately, diverting the entire
rest of the multi-stage run to paid cloud. Sticky-demotion is now gated on
REPEATED consecutive failures (AIFORGE_PRIMARY_DEMOTE_AFTER, default 2): a
lone blip escalates THAT call to cloud but the next call retries the local
primary; a success between failures resets the streak."""
from __future__ import annotations

import asyncio
import os

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types as gtypes

from aiforge_core.runtime.escalating_llm import EscalatingLlm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ.keys()):
        if k.startswith("AIFORGE_"):
            monkeypatch.delenv(k, raising=False)


def _resp(text: str = "ok") -> LlmResponse:
    return LlmResponse(content=gtypes.Content(
        role="model", parts=[gtypes.Part.from_text(text=text)]))


class _Programmable(BaseLlm):
    """Raises when ``fail`` is set, else yields ``out``. Both mutable so a
    test can flip the primary's behaviour between drive() calls."""
    fail: bool = False
    out: str = "primary_ok"
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.fail:
            raise RuntimeError("transient blip")
        yield _resp(self.out)

    @classmethod
    def supported_models(cls):
        return []


def _drive(model: BaseLlm) -> list[LlmResponse]:
    req = LlmRequest(model=model.model)

    async def _go():
        return [r async for r in model.generate_content_async(req, stream=False)]

    return asyncio.run(_go())


def _mk(primary, cloud):
    return EscalatingLlm(model="primary", role="doer",
                         primary_model=primary, chain_models=[cloud],
                         chain_labels=["cloud"])


def test_single_blip_does_not_demote() -> None:
    # Default threshold (2). One primary failure → cloud rescues THIS call
    # but primary is NOT sticky-demoted; the next call tries primary again.
    primary = _Programmable(model="primary", fail=True)
    cloud = _Programmable(model="cloud", fail=False, out="rescued")
    e = _mk(primary, cloud)

    out1 = _drive(e)
    assert out1[0].content.parts[0].text == "rescued"
    assert e.primary_demoted is False          # NOT demoted on one blip
    assert e.primary_fail_streak == 1

    # Next call: primary recovers → it is tried again (not skipped).
    primary.fail = False
    primary.out = "primary_back"
    prior = primary.calls
    out2 = _drive(e)
    assert out2[0].content.parts[0].text == "primary_back"
    assert primary.calls == prior + 1          # primary was retried
    assert e.primary_fail_streak == 0          # streak reset on success


def test_two_consecutive_failures_demote() -> None:
    primary = _Programmable(model="primary", fail=True)
    cloud = _Programmable(model="cloud", fail=False, out="rescued")
    e = _mk(primary, cloud)

    _drive(e)                                  # failure 1
    assert e.primary_demoted is False
    _drive(e)                                  # failure 2 → demote
    assert e.primary_demoted is True
    assert e.primary_fail_streak >= 2


def test_success_between_failures_resets_streak() -> None:
    primary = _Programmable(model="primary", fail=True)
    cloud = _Programmable(model="cloud", fail=False, out="rescued")
    e = _mk(primary, cloud)

    _drive(e)                                  # failure 1 → streak 1
    assert e.primary_fail_streak == 1

    primary.fail = False                       # success resets streak
    _drive(e)
    assert e.primary_fail_streak == 0
    assert e.primary_demoted is False

    primary.fail = True                        # failure again → streak 1, not 2
    _drive(e)
    assert e.primary_fail_streak == 1
    assert e.primary_demoted is False          # NOT demoted (streak reset earlier)


def test_env_override_demote_after_one(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_PRIMARY_DEMOTE_AFTER", "1")
    primary = _Programmable(model="primary", fail=True)
    cloud = _Programmable(model="cloud", fail=False, out="rescued")
    e = _mk(primary, cloud)
    _drive(e)
    assert e.primary_demoted is True           # threshold 1 → immediate demote
