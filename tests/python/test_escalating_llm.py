"""Tests for the cloud-escalation chain + EscalatingLlm wrapper.

ADK BaseLlm has a heavy import chain (google.genai, litellm, …) so the
tests stub out the real models and only exercise the retry decision
logic. Run with::

    pytest tests/python/test_escalating_llm.py -q
"""
from __future__ import annotations

import asyncio
import os
import types as _t

import pytest

from aiforge_core.config import agent_config as ac


# ─── cloud_escalation_chain — operator config logic ───────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every AIFORGE_*_PROVIDER + cloud-escalation env var so each
    test starts from a deterministic baseline."""
    for k in list(os.environ.keys()):
        if k.startswith(("AIFORGE_", "OLLAMA_CLOUD_", "ANTHROPIC_")):
            monkeypatch.delenv(k, raising=False)


def _force_primary_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ac, "get",
        lambda role: {"provider": "local", "model": "x", "base_url": None},
    )


def test_chain_skips_providers_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No keys configured → chain holds only claude_local (no key needed)."""
    _force_primary_local(monkeypatch)
    chain = ac.cloud_escalation_chain("doer")
    providers = [c["_provider"] for c in chain]
    assert "ollama_cloud" not in providers
    assert "anthropic" not in providers
    assert "claude_local" in providers


def test_chain_includes_ollama_when_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_primary_local(monkeypatch)
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    chain = ac.cloud_escalation_chain("doer")
    providers = [c["_provider"] for c in chain]
    assert "ollama_cloud" in providers
    # claude_local always present (CLI keychain auth).
    assert "claude_local" in providers


def test_chain_skips_primary_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When primary is anthropic, anthropic shouldn't appear in the chain."""
    monkeypatch.setattr(
        ac, "get",
        lambda role: {"provider": "anthropic", "model": "x", "base_url": None},
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    chain = ac.cloud_escalation_chain("doer")
    providers = [c["_provider"] for c in chain]
    assert "anthropic" not in providers
    assert "ollama_cloud" in providers


def test_chain_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_primary_local(monkeypatch)
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    assert ac.cloud_escalation_chain("doer") == []


def test_chain_pinned_provider_first(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_primary_local(monkeypatch)
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("AIFORGE_DOER_CLOUD_PROVIDER", "anthropic")
    chain = ac.cloud_escalation_chain("doer")
    providers = [c["_provider"] for c in chain]
    assert providers[0] == "anthropic"


# ─── EscalatingLlm — retry decision logic ─────────────────────────────


pytest.importorskip("google.adk")


from aiforge_core.runtime.escalating_llm import EscalatingLlm, _is_empty  # noqa: E402
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_request import LlmRequest  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.genai import types as gtypes  # noqa: E402


def _resp(text: str = "ok") -> LlmResponse:
    return LlmResponse(
        content=gtypes.Content(
            role="model", parts=[gtypes.Part.from_text(text=text)],
        ),
    )


class _StubModel(BaseLlm):
    """Configurable fake — yields the script you give it, or raises."""

    script: list = []
    error: BaseException | None = None
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.error:
            raise self.error
        for r in self.script:
            yield r

    @classmethod
    def supported_models(cls):
        return []


def _drive(model: BaseLlm, *, stream: bool = False) -> list[LlmResponse]:
    req = LlmRequest(model=model.model)

    async def _go() -> list[LlmResponse]:
        out: list[LlmResponse] = []
        async for r in model.generate_content_async(req, stream=stream):
            out.append(r)
        return out

    return asyncio.run(_go())


def test_is_empty_text() -> None:
    assert _is_empty(LlmResponse())  # no content
    assert _is_empty(_resp(""))      # empty text
    assert not _is_empty(_resp("hi"))


def test_primary_success_short_circuits() -> None:
    primary = _StubModel(model="primary", script=[_resp("hello")])
    cloud = _StubModel(model="cloud", script=[_resp("CLOUD")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    out = _drive(e)
    assert len(out) == 1 and out[0].content.parts[0].text == "hello"
    assert primary.calls == 1
    assert cloud.calls == 0  # never reached


def test_primary_exception_falls_through() -> None:
    primary = _StubModel(model="primary", error=RuntimeError("local down"))
    cloud = _StubModel(model="cloud", script=[_resp("rescued")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    out = _drive(e)
    assert out[0].content.parts[0].text == "rescued"
    assert cloud.calls == 1


def test_empty_primary_falls_through() -> None:
    primary = _StubModel(model="primary", script=[_resp("")])
    cloud = _StubModel(model="cloud", script=[_resp("rescued")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    out = _drive(e)
    assert out[0].content.parts[0].text == "rescued"


def test_all_fail_raises_last_exception() -> None:
    primary = _StubModel(model="primary", error=RuntimeError("p"))
    cloud = _StubModel(model="cloud", error=RuntimeError("c"))
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    with pytest.raises(RuntimeError):
        _drive(e)


def test_sticky_demotion_prefers_cloud_after_primary_fail() -> None:
    """First call: primary fails → demoted. Second call: cloud is tried
    BEFORE primary_retry, so a recovered primary doesn't pre-empt the
    cheaper cloud path that's already proven to work."""
    primary = _StubModel(model="primary", error=RuntimeError("flaky"))
    cloud = _StubModel(model="cloud", script=[_resp("rescued")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])

    out1 = _drive(e)
    assert out1[0].content.parts[0].text == "rescued"
    assert e.primary_demoted is True
    assert primary.calls == 1  # primary tried once, failed
    assert cloud.calls == 1

    # Cloud succeeds → primary_retry never reached.
    primary.error = None
    primary.script = [_resp("primary_back")]
    cloud.script = [_resp("cloud_again")]
    out2 = _drive(e)
    assert out2[0].content.parts[0].text == "cloud_again"
    assert primary.calls == 1  # demoted; cloud went first and worked
    assert cloud.calls == 2


def test_primary_retry_saves_run_when_cloud_unreachable() -> None:
    """Sticky-demoted primary still gets a last-chance shot after cloud
    fails — otherwise a single transient primary blip + a flaky cloud
    chain would deadlock the pipeline."""
    primary = _StubModel(model="primary", error=RuntimeError("first blip"))
    cloud = _StubModel(model="cloud", error=RuntimeError("cloud down"))
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])

    # Call 1: primary blips, cloud down → primary_retry also fails (still
    # erroring). Whole stack raises.
    with pytest.raises(RuntimeError):
        _drive(e)
    assert e.primary_demoted is True
    assert primary.calls == 2  # primary slot + primary_retry slot
    assert cloud.calls == 1

    # Call 2: primary recovers, cloud still down. primary skipped (demoted)
    # → cloud fails → primary_retry rescues. Demotion cleared on success.
    primary.error = None
    primary.script = [_resp("primary_recovered")]
    out = _drive(e)
    assert out[0].content.parts[0].text == "primary_recovered"
    assert e.primary_demoted is False  # cleared
    assert primary.calls == 3
    assert cloud.calls == 2


def test_streaming_bypasses_retry_chain() -> None:
    """Streaming mode honours primary directly — partial chunk re-emission
    across providers would violate the streaming contract, so the chain
    is intentionally skipped."""
    primary = _StubModel(model="primary", script=[_resp(""), _resp("late")])
    cloud = _StubModel(model="cloud", script=[_resp("rescued")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    out = _drive(e, stream=True)
    # Got primary's two responses; cloud never invoked.
    assert len(out) == 2
    assert cloud.calls == 0
