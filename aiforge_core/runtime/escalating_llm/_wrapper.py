"""The :class:`EscalatingLlm` wrapper — primary + cloud fallback chain.

Split out of the former single-module ``escalating_llm``; behaviour identical.
"""
from __future__ import annotations

import asyncio
import os as _os
import time as _time
from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

from aiforge_core.llm import endpoint_breaker as _breaker

from ._builder import _build_one, _mirror_to_langfuse
from ._policy import (
    _api_base_of,
    _attempt_retries,
    _demote_after,
    _is_empty,
    _is_transient_llm_error,
)
from ._quieting import log
from ._rescue import _RescueMixin
from ._streaming import _StreamMixin


async def _throttle_global(role: "str | None" = None) -> None:
    """Obey the operator's calls-per-minute ceiling on the PIPELINE path too.

    ADK agents never touch llm.client, so a ceiling enforced only there would
    throttle chat while team mode — the highest-volume path — sailed past it.
    The limiter is blocking, so it runs in a worker thread: sleeping on the
    event loop would stall every other agent in the same run, including the
    ones that are not waiting on budget.
    """
    try:
        from aiforge_core.llm import rate_limiter as _rl
        # NO `if global_rpm() <= 0: return` short-circuit. acquire_global also
        # serves a hold imposed by a SERVER that rejected us, which applies
        # even when the operator set no ceiling of their own — and 0 is the
        # setting most operators run. Skipping the call here made team mode,
        # the highest-volume path and the one most likely to have earned the
        # rejection, the only path that disobeyed a 429. It fast-returns on its
        # own when there is nothing to wait for.
        loop = asyncio.get_running_loop()
        # The one gateway (throttle only here: this path counts the send AFTER
        # the response, in _meter_record, so it can attach token usage).
        await loop.run_in_executor(None, lambda: _rl.govern_send(
            role=role,
            max_wait_s=float(_os.environ.get("AIFORGE_LLM_MAX_WAIT_S", "120")),
            meter=False))
    except Exception:  # noqa: BLE001 — a throttle must never break a call
        # Including the limiter giving up: acquire_global lets the call through
        # rather than raising, and even if that changes, one throttled call
        # must not fail a whole pipeline run (each candidate would re-throttle,
        # so a raise here costs 120s per candidate and then llm.exhausted).
        return


def _meter_record(role: str, model_name):
    """Count one PIPELINE request in the toolbar meter.

    ADK agents reach the endpoint through LiteLlm/httpx, never through
    ``llm.client._post`` where the meter lives — so team mode, the single
    highest-volume path, read ZERO on a meter whose route docstring and UI copy
    both promise "chat, pipeline, jobs, memory". Same reasoning (and the same
    place) as the Langfuse mirror right below: what does not come through the
    client has to be mirrored here. Never raises.

    Returns the meter token for :func:`_meter_fail`.
    """
    try:
        from aiforge_core.llm import call_meter as _meter
        return _meter.record(role=role, provider="openai_compatible",
                             model=str(model_name or ""))
    except Exception:  # noqa: BLE001 — metering must never break a call
        return None


def _meter_fail(token, exc: "BaseException | None" = None,
                reason: str | None = None) -> None:
    """Mark a counted PIPELINE request as having produced no answer.

    Mirrored here for the same reason the count is: the ADK path never reaches
    ``llm.client``, and a failure rate that reads zero for the highest-volume
    path is worse than no failure rate at all. An EMPTY response counts too —
    the pipeline treats it as a failed attempt and escalates to the next
    candidate, so the meter must not call it a success. Never raises.
    """
    if not token:
        # `record` returns None only when it could not count the send; counting
        # the failure then would put `failed` above `total`.
        return
    try:
        from aiforge_core.llm import call_meter as _meter
        if not reason:
            reason = type(exc).__name__ if exc is not None else "error"
        _meter.record_failure(token, str(reason)[:64])
    except Exception:  # noqa: BLE001 — metering must never break a call
        pass


def _api_key_of(model) -> str:
    """Best-effort API key for a built model — same shape as `_api_base_of`:
    ADK's LiteLlm keeps it in ``_additional_args``."""
    key = getattr(model, "api_key", None)
    if not key:
        extra = getattr(model, "_additional_args", None)
        if isinstance(extra, dict):
            key = extra.get("api_key")
    return str(key or "")


def _usage_of(responses: list) -> "tuple[int, int]":
    """(prompt, completion) tokens across ADK responses, as the provider
    reported them. Missing usage is 0, never a guess."""
    in_t = out_t = 0
    for r in responses or []:
        usage = getattr(r, "usage_metadata", None)
        if usage is None:
            continue
        in_t += int(getattr(usage, "prompt_token_count", 0) or 0)
        out_t += int(getattr(usage, "candidates_token_count", 0) or 0)
    return in_t, out_t


def _meter_tokens(role: str, in_t: int, out_t: int, token=None) -> None:
    """Provider-reported tokens for one PIPELINE response. Never raises."""
    try:
        from aiforge_core.llm import call_meter as _meter
        _meter.record_tokens(role, prompt_tokens=in_t, completion_tokens=out_t,
                             token=token)
    except Exception:  # noqa: BLE001 — accounting must never break a call
        pass


class EscalatingLlm(_RescueMixin, _StreamMixin, BaseLlm):
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


    def _record_spend(self, model_name: str, responses: list, token) -> None:
        """Meter + budget for one answered request.

        The meter is written FIRST on purpose: it cannot raise, while
        ``tracker.record`` can, and a tracker that is down must not also cost us
        the token counts. Both are best-effort — accounting never blocks a yield.
        """
        in_t, out_t = _usage_of(responses)
        if not (in_t or out_t):
            return
        _meter_tokens(self.role, in_t, out_t, token)
        try:
            from aiforge_core.runtime.budget import tracker
            tracker.record(role=self.role, model=model_name,
                           input_tokens=in_t, output_tokens=out_t)
        except Exception as exc:  # noqa: BLE001 — accounting is best-effort
            log.debug("budget.record failed: %s", exc)


    def _candidates(self) -> list[tuple[str, BaseLlm]]:
        """Attempt order: primary (skipped if sticky-demoted) → cloud chain →
        primary as last-resort retry.

        The trailing primary slot saves us from total-failure stalls when (a)
        the primary had a transient blip earlier in the same pipeline run AND
        (b) no cloud provider can rescue (no key, all 5xx, etc). It is also the
        only attempt for a primary that was demoted on a *prior* call — without
        it, sticky-demotion + cloud-down = deadlock.
        """
        out: list[tuple[str, BaseLlm]] = []
        if not self.primary_demoted and self.primary_model is not None:
            out.append(("primary", self.primary_model))
        else:
            log.info("llm.primary_skipped role=%s reason=sticky_demotion",
                     self.role)
        out.extend(zip(self.chain_labels, self.chain_models))
        if self.primary_model is not None:
            out.append(("primary_retry", self.primary_model))
        return [(label, m) for label, m in out if m is not None]

    def _stamp_request(self, llm_request: LlmRequest, model) -> LlmRequest:
        """The request this candidate should actually receive.

        ADK's LlmAgent stamps the request with the agent-bound model name (the
        EscalatingLlm wrapper's ``model`` field). When we forward to a cloud
        provider whose model_id differs, LiteLlm picks ``llm_request.model``
        FIRST (``or self.model``) and posts e.g. the local mlx-lm path to
        ollama.com → 404. Stamp the chain entry's model on each forward so the
        right id reaches the right endpoint, then apply the per-model quirk
        sheet (system suffix / token cap / temp) so it tracks whichever model is
        actually serving this call.
        """
        target = getattr(model, "model", None)
        req = llm_request
        if target and llm_request.model != target:
            req = llm_request.model_copy(update={"model": target})
        from aiforge_core.config import model_overrides
        req = model_overrides.apply(target, req, role=self.role)
        from aiforge_core.llm import reasoning as _reasoning
        api_base = (getattr(model, "_additional_args", None) or {}).get("api_base", "")
        if target and _reasoning.reasoning_off(target, api_base):
            req = _reasoning.no_think_request(req)
        return req

    async def _attempt(self, model, req: LlmRequest, label: str, target,
                       out: dict) -> list:
        """One candidate's responses, with bounded retry-with-backoff on the
        SAME endpoint for transient errors (flaky 401, 5xx, connection, timeout)
        BEFORE the caller falls through to the next candidate — so a proxy blip
        does not surface as an "agent error" in the UI.

        ``out["token"]`` carries the meter token of the final try out to the
        caller, which owes the meter an `empty` failure when nothing was said.
        Raises the last exception when every try failed.
        """
        tries = _attempt_retries()
        buffered: list[LlmResponse] = []
        for t in range(tries):
            try:
                buffered = []
                await _throttle_global(self.role)
                out["token"] = _meter_record(self.role, target)
                async for r in model.generate_content_async(req, stream=False):
                    buffered.append(r)
                return buffered
            except Exception as exc:  # noqa: BLE001
                # Every try is its own counted request, so every try that dies
                # is its own counted failure — including the ones this loop
                # swallows by retrying, which are precisely the invisible calls
                # the meter exists for.
                _meter_fail(out.get("token"), exc)
                if t + 1 < tries and _is_transient_llm_error(exc):
                    log.warning("llm.attempt_retry role=%s attempt=%s "
                                "try=%d/%d err=%.140s", self.role, label,
                                t + 1, tries, str(exc))
                    await asyncio.sleep(min(8.0, 0.5 * (2 ** t)) + 0.1)
                    continue
                raise
        return buffered


    def _note_success(self, label: str) -> None:
        """Flag bookkeeping for a candidate that answered."""
        # primary_retry success — clear the demotion so subsequent calls go back
        # to the fast path. The cloud excursion was enough; no need to keep
        # paying its latency.
        if label == "primary_retry":
            self.primary_demoted = False
        # Any successful primary call (including primary_retry) earns a fresh
        # recovery budget for the NEXT crash. Without this reset, recovery is
        # one-shot per pipeline lifetime — ONE-117 hit MLX crash 3× across a
        # 67min run; the 3rd crash exhausted because the flag was already burnt
        # by the 2nd recovery 5min earlier.
        if label in ("primary", "primary_retry"):
            self.lm_recovery_tried = False
            # A primary success clears the consecutive-failure streak so a later
            # isolated blip starts counting fresh (a success between two
            # failures must not compound into a demotion).
            self.primary_fail_streak = 0
        else:
            log.info("llm.escalated role=%s succeeded_via=%s "
                     "(primary_demoted=%s)", self.role, label,
                     self.primary_demoted)

    def _exhausted(self, last_exc):
        """Everything failed — re-raise primary's last exception if there was
        one, else a synthetic exhausted-chain error so the ADK runner's outer
        except can mark the ticket blocked."""
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

    def _note_empty(self, label: str, model, buffered: list, token) -> None:
        """A counted request that answered nothing. The pipeline treats it as a
        failed attempt (it demotes on it and escalates to the next candidate)
        and so must the meter — an endpoint returning empties looks perfectly
        healthy on a success-blind rate."""
        _meter_fail(token, reason="empty")
        log.warning("llm.attempt_empty role=%s attempt=%s model=%s "
                    "responses=%d", self.role, label,
                    getattr(model, "model", "?"), len(buffered))
        if label == "primary":
            self._record_primary_failure()


    async def _try_candidate(self, label, model, llm_request, t0, state: dict):
        """One candidate end to end: attempt, then the rescues. Yields the
        responses that answered; yielding nothing means "move to the next
        candidate". ``state`` accumulates the freshest failure and whether the
        call is finished."""
        req = self._stamp_request(llm_request, model)
        target = getattr(model, "model", None)
        base = _api_base_of(model)
        # Shared with the chat client: an endpoint that just failed to connect
        # is skipped for the cooldown, so the chain moves on at once instead of
        # every role, on every call, re-paying the connect budget on a dead host.
        skipped = _breaker.is_open(base)
        if skipped:
            log.info("llm.candidate_skipped role=%s attempt=%s reason=%s",
                     self.role, label, skipped)
            state["exc"] = ConnectionError(f"LLM endpoint unreachable: {skipped}")
            return
        meter: dict = {}
        try:
            buffered = await self._attempt(model, req, label, target, meter)
        except Exception as exc:  # noqa: BLE001
            if _breaker.is_connect_error(exc):
                _breaker.record_failure(base, str(exc))
            async for r in self._rescue_after_failure(exc, model, req, label,
                                                      target, t0, state):
                yield r
            return
        _breaker.record_success(base)

        if not buffered or all(_is_empty(r) for r in buffered):
            self._note_empty(label, model, buffered, meter.get("token"))
            return

        self._note_success(label)
        self._record_spend(getattr(model, "model", "") or label, buffered,
                           meter.get("token"))
        _mirror_to_langfuse(self.role, req, buffered,
                            getattr(model, "model", "") or label,
                            int((_time.monotonic() - t0) * 1000))
        state["done"] = True
        for r in buffered:
            yield r

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        if stream:
            # Closed in a `finally`, not left to the loop: a consumer that walks
            # away throws GeneratorExit in HERE, and an inner generator merely
            # abandoned is finalised later by the loop's asyncgen shutdown —
            # which cancels it, and a CancelledError at its `yield` is a counted
            # failure. Abandoning a stream the model answered fine is not one.
            inner = self._stream_primary(llm_request)
            try:
                async for r in inner:
                    yield r
            finally:
                await inner.aclose()
            return

        t0 = _time.monotonic()
        state: dict = {"exc": None, "done": False}
        for label, model in self._candidates():
            async for r in self._try_candidate(label, model, llm_request,
                                               t0, state):
                yield r
            if state["done"]:
                return
        self._exhausted(state["exc"])

    @classmethod
    def supported_models(cls) -> list[str]:
        # Don't auto-register in LlmRegistry — caller hands an instance to
        # LlmAgent(model=...) directly, the registry is bypassed.
        return []
