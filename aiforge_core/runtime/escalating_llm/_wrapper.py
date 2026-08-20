"""The :class:`EscalatingLlm` wrapper — primary + cloud fallback chain.

Split out of the former single-module ``escalating_llm``; behaviour identical.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

from ._quieting import log
from ._policy import (
    _api_base_of,
    _attempt_retries,
    _demote_after,
    _is_empty,
    _is_transient_llm_error,
)
from ._builder import _build_one, _mirror_to_langfuse


def _meter_record(role: str, model_name) -> None:
    """Count one PIPELINE request in the toolbar meter.

    ADK agents reach the endpoint through LiteLlm/httpx, never through
    ``llm.client._post`` where the meter lives — so team mode, the single
    highest-volume path, read ZERO on a meter whose route docstring and UI copy
    both promise "chat, pipeline, jobs, memory". Same reasoning (and the same
    place) as the Langfuse mirror right below: what does not come through the
    client has to be mirrored here. Never raises.
    """
    try:
        from aiforge_core.llm import call_meter as _meter
        _meter.record(role=role, provider="openai_compatible",
                      model=str(model_name or ""))
    except Exception:  # noqa: BLE001 — metering must never break a call
        pass


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
    # Consecutive primary-failure counter. Sticky-demotion only fires once
    # this reaches _demote_after() (default 2) — a lone transient blip
    # escalates that ONE call to cloud but leaves the primary in play for
    # the next call. Reset to 0 on any primary success. Plain instance int
    # (same non-locked idiom as primary_demoted; a fresh EscalatingLlm is
    # built per ticket so there's no cross-run sharing).
    primary_fail_streak: int = 0
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

    def _record_primary_failure(self) -> None:
        """A primary attempt failed. Increment the consecutive-failure
        streak and STICKY-demote only once it reaches the threshold — so a
        single blip escalates THIS call to cloud (the caller still
        ``continue``s down the chain) but the NEXT call retries the local
        primary. Repeated failures still demote and stay on cloud."""
        self.primary_fail_streak = int(self.primary_fail_streak or 0) + 1
        if self.primary_fail_streak >= _demote_after():
            self.primary_demoted = True

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        # Streaming path: trust primary, no retry magic.
        if stream:
            assert self.primary_model is not None
            _meter_record(self.role, getattr(self.primary_model, "model", None))
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
        import time as _time
        _t0 = _time.monotonic()
        for idx, (label, model) in enumerate(candidates):
            if model is None:
                continue
            # ADK's LlmAgent stamps the request with the agent-bound
            # model name (the EscalatingLlm wrapper's `model` field).
            # When we forward to a cloud provider whose model_id is
            # different, LiteLlm picks llm_request.model FIRST (`or
            # self.model`) and posts e.g. the local mlx-lm path to
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
                target_model, req_for_attempt, role=self.role)
            buffered: list[LlmResponse] = []
            try:
                # Bounded retry-with-backoff on the SAME endpoint for
                # transient errors (flaky 401, 5xx, connection, timeout)
                # BEFORE falling through to the next candidate — so a proxy
                # blip doesn't surface as an "agent error" in the UI.
                _tries = _attempt_retries()
                for _t in range(_tries):
                    try:
                        buffered = []
                        _meter_record(self.role, target_model)
                        async for r in model.generate_content_async(
                            req_for_attempt, stream=False,
                        ):
                            buffered.append(r)
                        break
                    except Exception as _ie:  # noqa: BLE001
                        if _t + 1 < _tries and _is_transient_llm_error(_ie):
                            log.warning(
                                "llm.attempt_retry role=%s attempt=%s "
                                "try=%d/%d err=%.140s", self.role, label,
                                _t + 1, _tries, str(_ie))
                            await asyncio.sleep(min(8.0, 0.5 * (2 ** _t)) + 0.1)
                            continue
                        raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                err_str = str(exc)
                log.warning(
                    "llm.attempt_failed role=%s attempt=%s model=%s "
                    "api_base=%s errtype=%s err=%s",
                    self.role, label, getattr(model, "model", "?"),
                    _api_base_of(model) or "?",
                    type(exc).__name__, err_str[:800],
                )
                # LM Studio MLX crash mid-pipeline ("model has crashed"
                # / "No models loaded") — force-reload the model and
                # retry the SAME attempt once before falling through
                # to the cloud chain. Without this, sticky-demotion
                # locks us off the local primary for the rest of the
                # ticket and a stress run starves on cloud rate limits.
                if (label in ("primary", "primary_retry")
                        and not self.lm_recovery_tried):
                    from .. import local_starter
                    if local_starter.looks_like_lm_crash(err_str):
                        self.lm_recovery_tried = True
                        api_base = _api_base_of(model)
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
                                    self._record_primary_failure()
                                continue
                            if buffered and not all(_is_empty(r) for r in buffered):
                                log.info(
                                    "llm.recovered role=%s after_lm_reload",
                                    self.role,
                                )
                                _mirror_to_langfuse(
                                    self.role, req_for_attempt, buffered,
                                    getattr(model, "model", "") or label,
                                    int((_time.monotonic() - _t0) * 1000))
                                for r in buffered:
                                    yield r
                                return
                if label == "primary":
                    self._record_primary_failure()
                continue

            if not buffered or all(_is_empty(r) for r in buffered):
                log.warning(
                    "llm.attempt_empty role=%s attempt=%s model=%s "
                    "responses=%d", self.role, label,
                    getattr(model, "model", "?"), len(buffered),
                )
                if label == "primary":
                    self._record_primary_failure()
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
                # A primary success clears the consecutive-failure streak so
                # a later isolated blip starts counting fresh (a success
                # between two failures must not compound into a demotion).
                self.primary_fail_streak = 0

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
            _mirror_to_langfuse(
                self.role, req_for_attempt, buffered,
                getattr(model, "model", "") or label,
                int((_time.monotonic() - _t0) * 1000))
            for r in buffered:
                yield r
            return

        # Everything failed — re-raise primary's last exception if there
        # was one, else surface a synthetic exhausted-chain error so the
        # ADK runner's outer except can mark the ticket blocked.
        log.error(
            "llm.exhausted role=%s primary+%d cloud all failed — last err: %s: %s",
            self.role, len(self.chain_models),
            type(last_exc).__name__ if last_exc else "none",
            str(last_exc)[:800] if last_exc else "(empty responses)",
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
