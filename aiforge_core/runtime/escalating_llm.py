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
    if cfg.get("api_base"):
        kwargs["api_base"] = cfg["api_base"]
    if cfg.get("api_key"):
        kwargs["api_key"] = cfg["api_key"]
    if cfg.get("custom_llm_provider"):
        kwargs["custom_llm_provider"] = cfg["custom_llm_provider"]
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
            buffered: list[LlmResponse] = []
            try:
                async for r in model.generate_content_async(
                    llm_request, stream=False,
                ):
                    buffered.append(r)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning(
                    "llm.attempt_failed role=%s attempt=%s model=%s err=%s",
                    self.role, label, getattr(model, "model", "?"),
                    str(exc)[:200],
                )
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

            if label != "primary":
                log.info(
                    "llm.escalated role=%s succeeded_via=%s "
                    "(primary_demoted=%s)",
                    self.role, label, self.primary_demoted,
                )
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
