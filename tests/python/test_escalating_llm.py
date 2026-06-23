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
    """No keys configured → chain is empty (every cloud provider needs a key)."""
    _force_primary_local(monkeypatch)
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    chain = ac.cloud_escalation_chain("doer")
    providers = [c["_provider"] for c in chain]
    assert "ollama_cloud" not in providers
    assert "anthropic" not in providers
    assert providers == []


def test_chain_includes_ollama_when_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_primary_local(monkeypatch)
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    chain = ac.cloud_escalation_chain("doer")
    providers = [c["_provider"] for c in chain]
    assert "ollama_cloud" in providers


def test_chain_skips_primary_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When primary is ollama_cloud, it shouldn't appear in its own chain."""
    monkeypatch.setattr(
        ac, "get",
        lambda role: {"provider": "ollama_cloud", "model": "x", "base_url": None},
    )
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    chain = ac.cloud_escalation_chain("doer")
    providers = [c["_provider"] for c in chain]
    assert "ollama_cloud" not in providers


def test_chain_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_primary_local(monkeypatch)
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    assert ac.cloud_escalation_chain("doer") == []


def test_chain_pinned_provider_first(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_primary_local(monkeypatch)
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    monkeypatch.setenv("AIFORGE_DOER_CLOUD_PROVIDER", "ollama_cloud")
    chain = ac.cloud_escalation_chain("doer")
    providers = [c["_provider"] for c in chain]
    assert providers[0] == "ollama_cloud"


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


def test_chain_attempt_uses_target_models_id() -> None:
    """When forwarding to a chain entry, the LlmRequest.model must be
    rewritten to the chain entry's id — otherwise LiteLlm posts the
    primary's model name to the cloud endpoint and 404s."""

    seen_models: list[str] = []

    class _Recorder(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            seen_models.append(llm_request.model)
            yield _resp("ok")

        @classmethod
        def supported_models(cls):
            return []

    primary = _StubModel(model="primary-id", error=RuntimeError("force_chain"))
    cloud = _Recorder(model="cloud-target-id")
    e = EscalatingLlm(model="primary-id", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    out = _drive(e)
    assert out[0].content.parts[0].text == "ok"
    assert seen_models == ["cloud-target-id"], seen_models


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


# ─── LM-crash mid-pipeline recovery ────────────────────────────────────


def test_looks_like_lm_crash_matches_known_signatures() -> None:
    from aiforge_core.runtime import local_starter as ls
    assert ls.looks_like_lm_crash(
        "OpenAIException - Error code: 400 - {'error': "
        "'The model has crashed without additional information. "
        "(Exit code: null)'}"
    )
    assert ls.looks_like_lm_crash(
        "No models loaded. Please load a model in the developer page "
        "or use the 'lms load' command."
    )


def test_looks_like_lm_crash_skips_unrelated_errors() -> None:
    from aiforge_core.runtime import local_starter as ls
    assert not ls.looks_like_lm_crash("rate limit exceeded")
    assert not ls.looks_like_lm_crash("connection refused")
    assert not ls.looks_like_lm_crash("")


def test_lm_crash_triggers_recovery_then_retry(monkeypatch) -> None:
    """Primary raises crash signature → local_starter.try_recover is
    invoked → primary retried inline → success short-circuits chain."""
    from aiforge_core.runtime import local_starter as ls
    ls.reset()
    recover_calls = {"n": 0}
    monkeypatch.setattr(ls, "try_recover",
                        lambda api_base: recover_calls.update(n=1) or True)

    class _CrashThenRecover(BaseLlm):
        api_base: str = "http://127.0.0.1:1234/v1"
        calls: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "litellm.BadRequestError - The model has crashed "
                    "without additional information. (Exit code: null)"
                )
            yield _resp("recovered output")

        @classmethod
        def supported_models(cls):
            return []

    primary = _CrashThenRecover(model="local-mlx")
    cloud = _StubModel(model="cloud", script=[_resp("CLOUD")])
    e = EscalatingLlm(model="local-mlx", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    out = _drive(e)
    assert recover_calls["n"] == 1
    assert out[0].content.parts[0].text == "recovered output"
    assert cloud.calls == 0  # cloud bypassed by inline recovery
    assert e.lm_recovery_tried is True


def test_lm_crash_recovery_capped_per_pipeline(monkeypatch) -> None:
    """Second crash in the same EscalatingLlm does NOT re-fire
    try_recover; falls through to cloud chain instead."""
    from aiforge_core.runtime import local_starter as ls
    ls.reset()
    recover_calls = {"n": 0}
    monkeypatch.setattr(ls, "try_recover",
                        lambda api_base: recover_calls.update(
                            n=recover_calls["n"] + 1) or True)
    primary = _StubModel(model="local",
                         error=RuntimeError("model has crashed"))
    cloud = _StubModel(model="cloud", script=[_resp("CLOUD")])
    e = EscalatingLlm(model="local", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    e.lm_recovery_tried = True  # simulate prior recovery this pipeline
    out = _drive(e)
    assert recover_calls["n"] == 0  # cap respected
    assert out[0].content.parts[0].text == "CLOUD"


def test_non_crash_error_bypasses_recovery(monkeypatch) -> None:
    """Plain rate-limit / connection error must NOT call try_recover —
    it should escalate to cloud as before."""
    from aiforge_core.runtime import local_starter as ls
    ls.reset()
    recover_calls = {"n": 0}
    monkeypatch.setattr(ls, "try_recover",
                        lambda api_base: recover_calls.update(
                            n=recover_calls["n"] + 1) or True)
    primary = _StubModel(model="local",
                         error=RuntimeError("connection refused"))
    cloud = _StubModel(model="cloud", script=[_resp("CLOUD")])
    e = EscalatingLlm(model="local", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    out = _drive(e)
    assert recover_calls["n"] == 0
    assert out[0].content.parts[0].text == "CLOUD"


def test_lm_recovery_flag_resets_on_primary_success() -> None:
    """After a successful primary call, lm_recovery_tried resets to
    False so the NEXT crash gets a fresh recovery attempt. ONE-117
    needed 3 recoveries across a 67min run; the original 1-shot cap
    burnt out by crash 2."""
    primary = _StubModel(model="primary", script=[_resp("ok")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[],
                      chain_labels=[])
    e.lm_recovery_tried = True   # simulate prior recovery
    out = _drive(e)
    assert out[0].content.parts[0].text == "ok"
    assert e.lm_recovery_tried is False  # reset on primary success


def test_lm_recovery_flag_resets_on_primary_retry_success() -> None:
    """primary_retry success also resets lm_recovery_tried + clears
    primary_demoted. Both knobs need to flip green together."""
    class _OnceFail(BaseLlm):
        calls: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            yield _resp("recovered")

        @classmethod
        def supported_models(cls):
            return []

    primary = _OnceFail(model="primary")
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[],
                      chain_labels=[])
    e.lm_recovery_tried = True
    out = _drive(e)
    assert out[0].content.parts[0].text == "recovered"
    assert e.lm_recovery_tried is False
    assert e.primary_demoted is False


def test_repair_json_truncated_tool_args():
    import json as _json

    from aiforge_core.runtime.escalating_llm import _repair_json
    # the real failure: unterminated string in tool-call arguments
    for bad in ('{"path": "Main.java", "content": "public class Main {',
                '{"cmd": "mvn package', '{"tools": ["java", "mvn"]'):
        _json.loads(_repair_json(bad))   # must not raise
    assert _repair_json("") == "{}"
    assert _repair_json("not json at all") == "{}"
    assert _json.loads(_repair_json('{"a": "b"}')) == {"a": "b"}


def test_transient_error_classification():
    from aiforge_core.runtime.escalating_llm import _is_transient_llm_error

    class AuthenticationError(Exception):
        pass
    assert _is_transient_llm_error(AuthenticationError("401 Authorization Required"))
    assert _is_transient_llm_error(TimeoutError("timed out"))
    assert _is_transient_llm_error(ConnectionError("connection reset"))
    assert _is_transient_llm_error(Exception("503 Service Unavailable"))
    assert not _is_transient_llm_error(ValueError("bad model id"))
    assert not _is_transient_llm_error(Exception("invalid request body"))


def test_cloud_chain_skips_none_default_provider(monkeypatch):
    """Audit fix: a cloud provider with no default model (openai_compatible)
    pinned as the target must NOT crash (None.startswith) — it's skipped."""
    monkeypatch.setattr(
        ac, "get",
        lambda role: {"provider": "local", "model": "x", "base_url": None},
    )
    monkeypatch.setenv("AIFORGE_DOER_CLOUD_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AIFORGE_OPENAI_COMPAT_API_KEY", "k")
    chain = ac.cloud_escalation_chain("doer")          # must not raise
    assert all(c["_provider"] != "openai_compatible" for c in chain)


def test_cloud_default_for_local_skips_none_default(monkeypatch):
    monkeypatch.setattr(
        ac, "get",
        lambda role: {"provider": "local", "model": "x", "base_url": None},
    )
    monkeypatch.setenv("AIFORGE_LOCAL_DEAD_FALLBACK", "openai_compatible")
    monkeypatch.setenv("AIFORGE_OPENAI_COMPAT_API_KEY", "k")
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    # openai_compatible skipped (no default model), no ollama key → None, no crash
    assert ac.cloud_default_for_local("doer") is None
