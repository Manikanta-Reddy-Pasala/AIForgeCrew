"""Self-healing ADK ``BaseLlm`` wrapper with cloud escalation.

Wraps a primary ADK model (LiteLlm against the operator's local mlx-lm,
or ClaudeSubscriptionLlm) and an ordered list of cloud fallbacks
(Ollama Cloud → Anthropic → Claude subscription). On failure of the
primary the wrapper transparently retries the same request against
each cloud entry in turn, so the agent loop never stalls on a flaky
local model.

Failure triggers (any of):

* primary raises any exception during ``generate_content_async``
* primary yields zero responses (mlx-lm tool_calls bug surface)
* primary's final response carries no text and no tool calls
  (model lost the plot — usually a hallucinated stop token)

The wrapper is intentionally non-streaming: ADK ``LlmAgent`` /
``LoopAgent`` request ``stream=False`` by default for the v6 pipeline.
If the caller asks for streaming we honour the primary directly without
the retry chain — partial-chunk re-emission across providers would
violate the streaming contract.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse


log = logging.getLogger("aiforge.escalating_llm")


def _is_empty(resp: LlmResponse) -> bool:
    """A 200-OK that's actually useless — no text, no tool calls."""
    if resp.error_code:
        return True
    content = getattr(resp, "content", None)
    if content is None:
        return True
    parts = getattr(content, "parts", None) or []
    has_signal = False
    for p in parts:
        text = getattr(p, "text", None)
        if text and text.strip():
            has_signal = True
            break
        if getattr(p, "function_call", None):
            has_signal = True
            break
    return not has_signal


def _build_one(cfg: dict[str, Any]) -> BaseLlm:
    """Construct a BaseLlm from a resolve_litellm-shaped dict.

    Recognised cfg keys (besides ``model_id``/``api_base``/``api_key``):

    * ``_claude_cli`` — route through ``ClaudeSubscriptionLlm`` instead
      of LiteLLM (the subscription CLI doesn't speak the OpenAI proto).
    * ``custom_llm_provider`` — override LiteLLM's URL/model auto-detect.
      Required for ollama.com (OpenAI-compat at ``/v1`` but LiteLLM
      misroutes to ``/api/generate`` without it).
    """
    if cfg.get("_claude_cli"):
        from .claude_subscription_llm import ClaudeSubscriptionLlm
        model_id = cfg["model_id"]
        if model_id.startswith("anthropic/"):
            model_id = model_id.split("/", 1)[1]
        return ClaudeSubscriptionLlm(model=model_id)
    from google.adk.models.lite_llm import LiteLlm
    kwargs: dict[str, Any] = {"model": cfg["model_id"]}
    api_base = cfg.get("api_base") or ""
    if api_base:
        kwargs["api_base"] = api_base
    if cfg.get("api_key"):
        kwargs["api_key"] = cfg["api_key"]
    if cfg.get("custom_llm_provider"):
        kwargs["custom_llm_provider"] = cfg["custom_llm_provider"]
    # Self-hosted HTTPS endpoint with a self-signed / internal cert: mirror
    # the urllib client's AIFORGE_LLM_SSL_VERIFY toggle for the LiteLLM
    # (ADK / Team-flow) path. LiteLLM passes ssl_verify through to its
    # httpx client; only relevant for https, only when explicitly disabled.
    # A custom CA bundle (AIFORGE_LLM_CA_BUNDLE / SSL_CERT_FILE /
    # REQUESTS_CA_BUNDLE) is honoured by httpx natively and keeps verify ON.
    if str(api_base).lower().startswith("https://"):
        from aiforge_core.llm import _ssl as _llm_ssl
        # Skip TLS verify for the model endpoint when: the per-role opt-out
        # is set (UI checkbox / stored insecure_tls), the global
        # AIFORGE_LLM_SSL_VERIFY toggle is off, OR the host is trusted-
        # internal (self-hosted LAN box). A CA bundle keeps verify ON, and
        # public hosts always verify. Mirrors openai_compatible.probe so
        # Test and real calls agree.
        if not _llm_ssl._ca_bundle() and (
            cfg.get("insecure_tls")
            or not _llm_ssl._verify_enabled()
            or _llm_ssl.auto_relax_internal(api_base)
        ):
            kwargs["ssl_verify"] = False
    return LiteLlm(**kwargs)


class EscalatingLlm(BaseLlm):
    """Primary ADK model + ordered cloud fallback chain.

    Pydantic-friendly: stores child models as plain attributes via
    ``model_config(arbitrary_types_allowed=True)`` (inherited).

    Sticky-demotion: once the primary fails for any reason, this wrapper
    flags itself ``_primary_demoted`` and SKIPS the primary on every
    subsequent call. This matters for the LoopAgent[Doer, Feedback]
    cycle — if the local model produced a broken plan on turn 1 (which
    Feedback rejected), spending another turn on the same flaky model
    just burns latency. We promote to cloud and stay there for the
    duration of this pipeline run. A fresh EscalatingLlm is built per
    ticket inside ``_build_pipeline``, so the demotion auto-resets
    between tickets.
    """

    role: str
    primary_model: BaseLlm | None = None
    chain_models: list[BaseLlm] = []
    chain_labels: list[str] = []
    primary_demoted: bool = False
    # One LM-crash auto-recovery attempt per pipeline run. Resets per
    # ticket (fresh EscalatingLlm is built per ticket in pipeline.py).
    # Without the cap a flapping LM Studio could trigger an SSH-load
    # storm; with the cap, we get one free recovery per ticket and
    # subsequent crashes fall through to the cloud chain as normal.
    lm_recovery_tried: bool = False

    @classmethod
    def build(cls, role: str, primary_cfg: dict[str, Any],
              chain_cfgs: list[dict[str, Any]]) -> "EscalatingLlm":
        primary = _build_one(primary_cfg)
        chain = [_build_one(c) for c in chain_cfgs]
        labels = [c.get("_provider", "?") for c in chain_cfgs]
        return cls(
            model=primary.model,  # required pydantic field on BaseLlm
            role=role,
            primary_model=primary,
            chain_models=chain,
            chain_labels=labels,
        )

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        # Streaming path: trust primary, no retry magic.
        if stream:
            assert self.primary_model is not None
            async for r in self.primary_model.generate_content_async(
                llm_request, stream=True,
            ):
                yield r
            return

        # Non-streaming: collect primary's responses, judge, retry on fail.
        # Order: primary (skipped if sticky-demoted) → cloud chain →
        # primary as last-resort retry. The trailing primary slot saves
        # us from total-failure stalls when (a) the primary had a
        # transient blip earlier in the same pipeline run AND (b) no
        # cloud provider can rescue (no key, all 5xx, etc). It's also
        # the only attempt for a primary that was demoted on a *prior*
        # call — without it, sticky-demotion + cloud-down = deadlock.
        candidates: list[tuple[str, BaseLlm]] = []
        was_demoted_at_start = self.primary_demoted
        if not was_demoted_at_start and self.primary_model is not None:
            candidates.append(("primary", self.primary_model))
        for label, m in zip(self.chain_labels, self.chain_models):
            candidates.append((label, m))
        if self.primary_model is not None:
            candidates.append(("primary_retry", self.primary_model))

        if was_demoted_at_start:
            log.info(
                "llm.primary_skipped role=%s reason=sticky_demotion",
                self.role,
            )

        last_exc: Exception | None = None
        for idx, (label, model) in enumerate(candidates):
            if model is None:
                continue
            # ADK's LlmAgent stamps the request with the agent-bound
            # model name (the EscalatingLlm wrapper's `model` field).
            # When we forward to a cloud provider whose model_id is
            # different, LiteLlm picks llm_request.model FIRST (`or
            # self.model`) and posts e.g. `claude-opus-4-7` to
            # ollama.com → 404. Stamp the chain entry's model on each
            # forward so the right id reaches the right endpoint.
            req_for_attempt = llm_request
            target_model = getattr(model, "model", None)
            if target_model and llm_request.model != target_model:
                req_for_attempt = llm_request.model_copy(
                    update={"model": target_model},
                )
            # Per-model quirk sheet (system suffix / token cap / temp)
            # — applied per attempt so it tracks whichever model is
            # actually serving this call.
            from aiforge_core.config import model_overrides
            req_for_attempt = model_overrides.apply(
                target_model, req_for_attempt)
            buffered: list[LlmResponse] = []
            try:
                async for r in model.generate_content_async(
                    req_for_attempt, stream=False,
                ):
                    buffered.append(r)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                err_str = str(exc)
                log.warning(
                    "llm.attempt_failed role=%s attempt=%s model=%s err=%s",
                    self.role, label, getattr(model, "model", "?"),
                    err_str[:200],
                )
                # LM Studio MLX crash mid-pipeline ("model has crashed"
                # / "No models loaded") — force-reload the model and
                # retry the SAME attempt once before falling through
                # to the cloud chain. Without this, sticky-demotion
                # locks us off the local primary for the rest of the
                # ticket and a stress run starves on cloud rate limits.
                if (label in ("primary", "primary_retry")
                        and not self.lm_recovery_tried):
                    from . import local_starter
                    if local_starter.looks_like_lm_crash(err_str):
                        self.lm_recovery_tried = True
                        api_base = getattr(model, "api_base", "") or ""
                        recovered = local_starter.try_recover(api_base)
                        log.warning(
                            "llm.lm_crash_recovery role=%s recovered=%s",
                            self.role, recovered,
                        )
                        if recovered:
                            buffered = []
                            try:
                                async for r in model.generate_content_async(
                                    req_for_attempt, stream=False,
                                ):
                                    buffered.append(r)
                            except Exception as retry_exc:  # noqa: BLE001
                                last_exc = retry_exc
                                log.warning(
                                    "llm.recovery_retry_failed role=%s "
                                    "err=%s", self.role,
                                    str(retry_exc)[:200],
                                )
                                if label == "primary":
                                    self.primary_demoted = True
                                continue
                            if buffered and not all(_is_empty(r) for r in buffered):
                                log.info(
                                    "llm.recovered role=%s after_lm_reload",
                                    self.role,
                                )
                                for r in buffered:
                                    yield r
                                return
                if label == "primary":
                    self.primary_demoted = True
                continue

            if not buffered or all(_is_empty(r) for r in buffered):
                log.warning(
                    "llm.attempt_empty role=%s attempt=%s model=%s "
                    "responses=%d", self.role, label,
                    getattr(model, "model", "?"), len(buffered),
                )
                if label == "primary":
                    self.primary_demoted = True
                continue

            # primary_retry success — clear the demotion so subsequent
            # calls go back to the fast path. The cloud excursion was
            # enough; no need to keep paying its latency.
            if label == "primary_retry":
                self.primary_demoted = False

            # Any successful primary call (including primary_retry)
            # earns a fresh recovery budget for the NEXT crash. Without
            # this reset, recovery is one-shot per pipeline lifetime —
            # ONE-117 hit MLX crash 3× across a 67min run; the 3rd
            # crash exhausted because the flag was already burnt by
            # the 2nd recovery 5min earlier.
            if label in ("primary", "primary_retry"):
                self.lm_recovery_tried = False

            if label != "primary":
                log.info(
                    "llm.escalated role=%s succeeded_via=%s "
                    "(primary_demoted=%s)",
                    self.role, label, self.primary_demoted,
                )
            # Sub #9: record per-call spend on the unified budget tracker.
            # Best-effort: a missing usage_metadata field never blocks the
            # yield. Cost stays 0 — populated by a downstream price-table
            # plugin in a follow-up.
            try:
                from aiforge_core.runtime.budget import tracker
                in_t = 0
                out_t = 0
                for r in buffered:
                    usage = getattr(r, "usage_metadata", None)
                    if usage is None:
                        continue
                    in_t += int(
                        getattr(usage, "prompt_token_count", 0) or 0,
                    )
                    out_t += int(
                        getattr(usage, "candidates_token_count", 0) or 0,
                    )
                if in_t or out_t:
                    tracker.record(
                        role=self.role,
                        model=getattr(model, "model", "") or label,
                        input_tokens=in_t, output_tokens=out_t,
                    )
            except Exception as exc:  # noqa: BLE001 — accounting is best-effort
                log.debug("budget.record failed: %s", exc)
            for r in buffered:
                yield r
            return

        # Everything failed — re-raise primary's last exception if there
        # was one, else surface a synthetic exhausted-chain error so the
        # ADK runner's outer except can mark the ticket blocked.
        log.error(
            "llm.exhausted role=%s primary+%d cloud all failed",
            self.role, len(self.chain_models),
        )
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            f"EscalatingLlm exhausted: role={self.role} "
            f"primary+{len(self.chain_models)} cloud all empty"
        )

    @classmethod
    def supported_models(cls) -> list[str]:
        # Don't auto-register in LlmRegistry — caller hands an instance to
        # LlmAgent(model=...) directly, the registry is bypassed.
        return []
