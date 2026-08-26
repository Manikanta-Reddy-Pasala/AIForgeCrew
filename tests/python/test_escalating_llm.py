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


def test_chain_empty_no_cloud_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai_compatible is the only provider now and there is no built-in
    cloud chain — even with legacy keys set, the chain stays empty."""
    _force_primary_local(monkeypatch)
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    chain = ac.cloud_escalation_chain("doer")
    assert chain == []


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


def test_chain_pinned_unknown_provider_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pin to a provider that no longer exists (ollama_cloud) is skipped
    cleanly — the chain stays empty rather than crashing."""
    _force_primary_local(monkeypatch)
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "k")
    monkeypatch.setenv("AIFORGE_DOER_CLOUD_PROVIDER", "ollama_cloud")
    chain = ac.cloud_escalation_chain("doer")
    assert chain == []


# ─── EscalatingLlm — retry decision logic ─────────────────────────────




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
    assert len(out) == 1
    assert out[0].content.parts[0].text == "hello"
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


def test_sticky_demotion_prefers_cloud_after_primary_fail(monkeypatch) -> None:
    """First call: primary fails → demoted. Second call: cloud is tried
    BEFORE primary_retry, so a recovered primary doesn't pre-empt the
    cheaper cloud path that's already proven to work.

    Pinned to demote-after-1 (AIFORGE_PRIMARY_DEMOTE_AFTER=1) so this test
    validates the sticky-demotion ROUTING independently of the default
    streak threshold (which is 2 — see test_demote_streak)."""
    monkeypatch.setenv("AIFORGE_PRIMARY_DEMOTE_AFTER", "1")
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


def test_primary_retry_saves_run_when_cloud_unreachable(monkeypatch) -> None:
    """Sticky-demoted primary still gets a last-chance shot after cloud
    fails — otherwise a single transient primary blip + a flaky cloud
    chain would deadlock the pipeline.

    Pinned to demote-after-1 so the demotion assertions test the routing,
    not the default streak threshold (see test_demote_streak for that)."""
    monkeypatch.setenv("AIFORGE_PRIMARY_DEMOTE_AFTER", "1")
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


# ─── the meter mirror: the ADK path never touches llm.client ──────────
# Anything counted or enforced at llm.client has to be mirrored here or the
# highest-volume path (team mode) reports zero. That is true of the FAILURE
# count too: a failure rate blind to the pipeline says "all healthy" while
# every agent in a run is retrying.


@pytest.fixture
def _meter():
    from aiforge_core.llm import call_meter
    call_meter.reset_all()
    yield call_meter
    call_meter.reset_all()


def test_pipeline_failures_are_counted_beside_pipeline_requests(_meter) -> None:
    primary = _StubModel(model="primary", error=RuntimeError("local down"))
    cloud = _StubModel(model="cloud", script=[_resp("rescued")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    assert _drive(e)[0].content.parts[0].text == "rescued"

    g = _meter.global_snapshot(series=False)
    assert g["total"] == 2                # both attempts went out…
    assert g["failed"] == 1               # …one of them answered nothing
    assert g["by_fail_reason"] == {"RuntimeError": 1}


def test_an_empty_pipeline_answer_counts_as_a_failure(_meter) -> None:
    """The pipeline treats an empty response as a failed attempt — it demotes
    on it and escalates. A meter that called it a success would show a model
    returning nothing all day as perfectly healthy."""
    primary = _StubModel(model="primary", script=[_resp("")])
    cloud = _StubModel(model="cloud", script=[_resp("rescued")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[cloud],
                      chain_labels=["cloud"])
    _drive(e)
    g = _meter.global_snapshot(series=False)
    assert g["total"] == 2
    assert g["failed"] == 1
    assert g["by_fail_reason"] == {"empty": 1}


def test_a_healthy_pipeline_call_reports_no_failures(_meter) -> None:
    primary = _StubModel(model="primary", script=[_resp("hello")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[],
                      chain_labels=[])
    _drive(e)
    g = _meter.global_snapshot(series=False)
    assert g["total"] == 1
    assert g["failed"] == 0
    assert g["failed_per_minute"] == 0


def test_a_failed_stream_is_counted(_meter) -> None:
    primary = _StubModel(model="primary", error=RuntimeError("stream down"))
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[],
                      chain_labels=[])
    with pytest.raises(RuntimeError):
        _drive(e, stream=True)
    g = _meter.global_snapshot(series=False)
    assert g["total"] == 1
    assert g["failed"] == 1


def test_abandoning_a_stream_is_not_a_failure(_meter) -> None:
    """A consumer that stops iterating throws GeneratorExit into the wrapper.
    The model answered; walking away from the rest is not a failed request."""
    primary = _StubModel(model="primary",
                         script=[_resp("one"), _resp("two"), _resp("three")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[],
                      chain_labels=[])

    async def _go():
        agen = e.generate_content_async(LlmRequest(model="primary"), stream=True)
        async for _r in agen:
            break                      # take one, walk away
        await agen.aclose()            # → GeneratorExit inside the wrapper

    asyncio.run(_go())
    g = _meter.global_snapshot(series=False)
    assert g["total"] == 1
    assert g["failed"] == 0


def test_the_lm_crash_recovery_retry_is_counted_too(monkeypatch, _meter) -> None:
    """The recovery retry is a real request to the model. Every meter call on
    that path was invisible to the tests: deleting the record, the failure, or
    both left the suite green while a crash-loop-and-recover box under-reported
    its traffic."""
    from aiforge_core.runtime import local_starter as ls
    ls.reset()
    monkeypatch.setattr(ls, "try_recover", lambda api_base: True)

    class _CrashThenRecover(BaseLlm):
        api_base: str = "http://127.0.0.1:1234/v1"
        calls: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "litellm.BadRequestError - The model has crashed "
                    "without additional information. (Exit code: null)")
            yield _resp("recovered output")

        @classmethod
        def supported_models(cls):
            return []

    e = EscalatingLlm(model="local-mlx", role="doer",
                      primary_model=_CrashThenRecover(model="local-mlx"),
                      chain_models=[], chain_labels=[])
    assert _drive(e)[0].content.parts[0].text == "recovered output"

    g = _meter.global_snapshot(series=False)
    assert g["total"] == 2          # the crashed attempt AND the recovery retry
    assert g["failed"] == 1         # the crash
    assert g["by_fail_reason"] == {"RuntimeError": 1}


def test_a_recovery_retry_that_answers_nothing_is_counted_as_empty(
        monkeypatch, _meter) -> None:
    from aiforge_core.runtime import local_starter as ls
    ls.reset()
    monkeypatch.setattr(ls, "try_recover", lambda api_base: True)

    class _CrashThenEmpty(BaseLlm):
        api_base: str = "http://127.0.0.1:1234/v1"
        calls: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("The model has crashed (Exit code: null)")
            yield _resp("")          # recovered, still answering nothing

        @classmethod
        def supported_models(cls):
            return []

    e = EscalatingLlm(model="local-mlx", role="doer",
                      primary_model=_CrashThenEmpty(model="local-mlx"),
                      chain_models=[_StubModel(model="cloud",
                                               script=[_resp("rescued")])],
                      chain_labels=["cloud"])
    _drive(e)
    g = _meter.global_snapshot(series=False)
    assert g["failed"] == 2
    assert g["by_fail_reason"].get("empty") == 1


def test_a_think_only_stream_is_not_a_healthy_request(_meter) -> None:
    """Chunk-counting called a think-only stream healthy while the
    non-streaming path called the identical reply `empty` — and think-only
    output is the documented-common local-model failure."""
    primary = _StubModel(model="primary",
                         script=[_resp("<think>never answers</think>")])
    e = EscalatingLlm(model="primary", role="doer",
                      primary_model=primary, chain_models=[],
                      chain_labels=[])
    _drive(e, stream=True)
    g = _meter.global_snapshot(series=False)
    assert g["total"] == 1
    assert g["failed"] == 1
    assert g["by_fail_reason"] == {"empty": 1}


def test_team_mode_falls_back_to_a_model_the_box_serves(monkeypatch, _meter):
    """The rescue mirrored onto the ADK path. Without it team mode is the one
    path that still dies on a stale line of config while simple chat recovers —
    and it is the expensive path to lose."""
    from aiforge_core.llm.client import _models
    import json as _json

    class _Resp:
        def read(self):
            return _json.dumps(
                {"data": [{"id": "qwen/qwen3-coder-next"}]}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False

    _models.reset_cache()
    monkeypatch.setattr(_models.urllib.request, "urlopen",
                        lambda *_a, **_k: _Resp())

    class _MissingThenServed(BaseLlm):
        api_base: str = "http://127.0.0.1:1234/v1"
        seen: list = []

        async def generate_content_async(self, llm_request, stream=False):
            self.seen.append(llm_request.model)
            if llm_request.model != "qwen/qwen3-coder-next":
                raise RuntimeError(
                    "litellm.BadRequestError - No models loaded. Please load a "
                    "model in the developer page")
            yield _resp("rescued by the stand-in")

        @classmethod
        def supported_models(cls):
            return []

    primary = _MissingThenServed(model="qwen/qwen3.6-27b", seen=[])
    e = EscalatingLlm(model="qwen/qwen3.6-27b", role="doer",
                      primary_model=primary, chain_models=[],
                      chain_labels=[])
    out = _drive(e)
    assert out[0].content.parts[0].text == "rescued by the stand-in"
    assert primary.seen[-1] == "qwen/qwen3-coder-next"
    _models.reset_cache()


def test_a_box_that_is_simply_down_is_not_substituted(monkeypatch, _meter):
    """"Connection refused" says nothing about the model id. Probing on every
    failure would send an outbound request from every dead-endpoint run."""
    from aiforge_core.llm.client import _models

    _models.reset_cache()
    probed = {"n": 0}

    def _boom(*_a, **_k):
        probed["n"] += 1
        raise OSError("refused")

    monkeypatch.setattr(_models.urllib.request, "urlopen", _boom)
    # The stub MUST carry an api_base. Without one `_substitute_model` returns
    # at the "no endpoint" guard before it ever reaches the probe, so the test
    # passed no matter what the rule under test did — deleting
    # `_looks_like_missing_model` changed nothing.
    class _Down(BaseLlm):
        api_base: str = "http://127.0.0.1:1234/v1"

        async def generate_content_async(self, llm_request, stream=False):
            raise ConnectionError("refused")
            yield  # pragma: no cover — makes this an async generator

        @classmethod
        def supported_models(cls):
            return []

    primary = _Down(model="qwen/x")
    cloud = _StubModel(model="cloud", script=[_resp("cloud answer")])
    e = EscalatingLlm(model="qwen/x", role="doer", primary_model=primary,
                      chain_models=[cloud], chain_labels=["cloud"])
    assert _drive(e)[0].content.parts[0].text == "cloud answer"
    assert probed["n"] == 0
    _models.reset_cache()


def _missing_model_stub(base_url="http://127.0.0.1:1234/v1"):
    # NOTE: the parameter cannot be called `api_base` — assigning to that name
    # inside the class body makes the RHS lookup local to it, and the default
    # raises NameError before a single test runs.
    class _MissingThenServed(BaseLlm):
        api_base: str = base_url
        seen: list = []

        async def generate_content_async(self, llm_request, stream=False):
            self.seen.append(llm_request.model)
            if llm_request.model != "qwen/qwen3-coder-next":
                raise RuntimeError(
                    "litellm.BadRequestError - No models loaded.")
            yield _resp("stand-in answer")

        @classmethod
        def supported_models(cls):
            return []
    return _MissingThenServed


def _probe(monkeypatch, ids=("qwen/qwen3-coder-next",), seen=None):
    from aiforge_core.llm.client import _models
    import json as _json
    _models.reset_cache()

    class _R:
        def read(self):
            return _json.dumps({"data": [{"id": i} for i in ids]}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False

    def _open(req, timeout=None):
        if seen is not None:
            seen.append(req)
        return _R()

    monkeypatch.setattr(_models.urllib.request, "urlopen", _open)
    return _models


def test_the_kill_switch_stops_the_team_substitution_too(monkeypatch, _meter):
    """The operator who sets AUTOFALLBACK=0 wants a wrong model to be a hard
    failure. Honouring that in chat and ignoring it in team mode is the silent
    substitution the flag exists to prevent, on the path that runs a whole
    ticket."""
    monkeypatch.setenv("AIFORGE_LLM_MODEL_AUTOFALLBACK", "0")
    _models = _probe(monkeypatch)
    primary = _missing_model_stub()(model="qwen/qwen3.6-27b", seen=[])
    e = EscalatingLlm(model="qwen/qwen3.6-27b", role="doer",
                      primary_model=primary, chain_models=[], chain_labels=[])
    with pytest.raises(RuntimeError):
        _drive(e)
    assert "qwen/qwen3-coder-next" not in primary.seen
    _models.reset_cache()


def test_the_probe_carries_the_endpoint_key(monkeypatch, _meter):
    """/v1/models is authenticated on most hosted endpoints. An unauthenticated
    probe 401s, concludes nothing, and the rescue silently never fires."""
    monkeypatch.delenv("AIFORGE_LLM_MODEL_AUTOFALLBACK", raising=False)
    reqs: list = []
    _models = _probe(monkeypatch, seen=reqs)
    # LiteLlm keeps the key in `_additional_args`, which is exactly why
    # `_api_key_of` looks there — a pydantic BaseLlm rejects a stray attribute.
    cls = _missing_model_stub()
    primary = cls(model="qwen/qwen3.6-27b", seen=[])
    object.__setattr__(primary, "_additional_args", {"api_key": "sk-secret"})
    e = EscalatingLlm(model="qwen/qwen3.6-27b", role="doer",
                      primary_model=primary, chain_models=[], chain_labels=[])
    _drive(e)
    assert reqs
    assert reqs[0].get_header("Authorization") == "Bearer sk-secret"
    _models.reset_cache()


def test_a_cloud_candidate_is_never_substituted(monkeypatch, _meter):
    """A cloud 404 for a decommissioned id must not be re-issued against
    whatever a proxy happens to serve — that is a billed generation on a model
    nobody chose. Only the primary is rescued."""
    monkeypatch.delenv("AIFORGE_LLM_MODEL_AUTOFALLBACK", raising=False)
    _models = _probe(monkeypatch)
    primary = _StubModel(model="local", error=RuntimeError("boom"))
    cloud = _missing_model_stub("https://api.example.com/v1")(
        model="gpt-old", seen=[])
    e = EscalatingLlm(model="local", role="doer", primary_model=primary,
                      chain_models=[cloud], chain_labels=["cloud"])
    with pytest.raises(RuntimeError):
        _drive(e)
    assert "qwen/qwen3-coder-next" not in cloud.seen
    _models.reset_cache()


def test_a_rescued_team_response_is_counted_with_its_tokens(monkeypatch, _meter):
    """The substitution path used to yield and return before the accounting
    block, so a rescued run counted a request and zero tokens — on the meter
    added precisely because team mode is the highest-volume writer."""
    monkeypatch.delenv("AIFORGE_LLM_MODEL_AUTOFALLBACK", raising=False)
    _models = _probe(monkeypatch)

    class _Usage:
        prompt_token_count = 400
        candidates_token_count = 150

    class _WithUsage(BaseLlm):
        api_base: str = "http://127.0.0.1:1234/v1"

        async def generate_content_async(self, llm_request, stream=False):
            if llm_request.model != "qwen/qwen3-coder-next":
                raise RuntimeError("litellm.BadRequestError - No models loaded.")
            r = _resp("stand-in answer")
            r.usage_metadata = _Usage()      # type: ignore[attr-defined]
            yield r

        @classmethod
        def supported_models(cls):
            return []

    e = EscalatingLlm(model="qwen/qwen3.6-27b", role="doer",
                      primary_model=_WithUsage(model="qwen/qwen3.6-27b"),
                      chain_models=[], chain_labels=[])
    assert _drive(e)[0].content.parts[0].text == "stand-in answer"
    g = _meter.global_snapshot(series=False)
    assert g["tokens_out"] == 150
    assert g["tokens_in"] == 400
    _models.reset_cache()


def test_team_mode_uses_the_operators_configured_models(monkeypatch, _meter):
    """"I added four models; use the others when one dies" has to mean the same
    thing in team mode. The pipeline path only ever substituted a model the
    endpoint reported as SERVED, which is a different (and narrower) question
    than "what did the operator configure"."""
    from aiforge_core.config import model_registry

    monkeypatch.delenv("AIFORGE_LLM_MODEL_AUTOFALLBACK", raising=False)
    monkeypatch.setattr(model_registry, "_load", lambda: [
        {"id": "a", "model": "qwen/dead", "base_url": "", "api_key": ""},
        {"id": "b", "model": "qwen/alive", "base_url": "", "api_key": ""},
    ])

    class _DeadThenAlive(BaseLlm):
        api_base: str = "http://127.0.0.1:1234/v1"
        seen: list = []

        async def generate_content_async(self, llm_request, stream=False):
            self.seen.append(llm_request.model)
            if llm_request.model != "qwen/alive":
                # SERVED, just not answering — not a "missing model" error.
                raise RuntimeError("litellm.APIConnectionError: read timeout")
            yield _resp("answered by the configured fallback")

        @classmethod
        def supported_models(cls):
            return []

    primary = _DeadThenAlive(model="qwen/dead", seen=[])
    e = EscalatingLlm(model="qwen/dead", role="doer", primary_model=primary,
                      chain_models=[], chain_labels=[])
    out = _drive(e)
    assert out[0].content.parts[0].text == "answered by the configured fallback"
    assert primary.seen[-1] == "qwen/alive"


def test_a_registry_row_on_ANOTHER_host_is_skipped_in_team_mode(monkeypatch, _meter):
    """The ADK request is bound to this agent's own client, so a row pointing
    at a different endpoint cannot be honoured here — sending its model id to
    THIS host would just be a second wrong model."""
    from aiforge_core.config import model_registry

    monkeypatch.delenv("AIFORGE_LLM_MODEL_AUTOFALLBACK", raising=False)
    monkeypatch.setattr(model_registry, "_load", lambda: [
        {"id": "a", "model": "qwen/dead", "base_url": "", "api_key": ""},
        {"id": "b", "model": "cloud/only", "base_url": "https://api.other/v1",
         "api_key": "sk-x"},
    ])

    class _AlwaysDead(BaseLlm):
        api_base: str = "http://127.0.0.1:1234/v1"
        seen: list = []

        async def generate_content_async(self, llm_request, stream=False):
            self.seen.append(llm_request.model)
            raise RuntimeError("litellm.APIConnectionError: read timeout")
            yield  # pragma: no cover

        @classmethod
        def supported_models(cls):
            return []

    primary = _AlwaysDead(model="qwen/dead", seen=[])
    cloud = _StubModel(model="cloud", script=[_resp("cloud answer")])
    e = EscalatingLlm(model="qwen/dead", role="doer", primary_model=primary,
                      chain_models=[cloud], chain_labels=["cloud"])
    assert _drive(e)[0].content.parts[0].text == "cloud answer"
    assert "cloud/only" not in primary.seen
