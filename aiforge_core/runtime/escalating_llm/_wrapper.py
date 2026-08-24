"""The :class:`EscalatingLlm` wrapper — primary + cloud fallback chain.

Split out of the former single-module ``escalating_llm``; behaviour identical.
"""
from __future__ import annotations

import asyncio
import os as _os
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
    _looks_like_missing_model,
)
from ._builder import _build_one, _mirror_to_langfuse


async def _throttle_global() -> None:
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
        await loop.run_in_executor(None, lambda: _rl.acquire_global(
            max_wait_s=float(_os.environ.get("AIFORGE_LLM_MAX_WAIT_S", "120"))))
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

    async def _substitute_model(self, exc, model, req, label, meta: dict):  # noqa: C901
        """Re-issue ONE attempt against a model this endpoint actually serves.

        Yields the responses when the stand-in worked and nothing when it did
        not — the caller then falls through to the cloud chain exactly as
        before. LiteLlm picks ``llm_request.model`` before its own, so the
        substitution is a stamped request, not a rebuilt model object.
        """
        # WHICH models to stand in with, in order:
        #   1. the operator's OTHER configured models (the registry) — the
        #      same chain the chat path walks, so "I added four models, use
        #      the others when one dies" means the same thing in team mode;
        #   2. failing that, whatever this endpoint reports as served, which
        #      is the only option when the failure is "that model is not
        #      loaded here".
        # Trigger differs by source: a MISSING model is a config error and
        # every candidate is worth trying, while a model that is served but
        # not answering only justifies the registry chain — re-rolling through
        # every id the box happens to have loaded turns one dead model into a
        # sweep of the whole host.
        _missing = _looks_like_missing_model(exc)
        if not (_missing or _is_transient_llm_error(exc)):
            return
        # The operator's kill switch applies HERE too. The direct-client rescue
        # is gated on it and documents why: someone comparing models wants a
        # wrong id to be a hard failure. Honouring it in chat and ignoring it in
        # team mode is the same silent substitution the flag exists to prevent,
        # on the path that runs a whole ticket.
        try:
            from aiforge_core.llm.client import _autofallback_enabled
            if not _autofallback_enabled():
                return
        except Exception:  # noqa: BLE001
            pass
        base = _api_base_of(model)
        if not base:
            return
        _mid = getattr(model, "model", "") or ""
        sub = ""
        try:
            from aiforge_core.config import model_registry as _mr
            for _row in _mr.chain_after(_mid, base):
                if isinstance(_row, dict) and str(_row.get("model") or "").strip():
                    # Registry rows that name ANOTHER host are for the text
                    # path, which can rebuild the endpoint. Here the request is
                    # bound to this agent's own client, so only a different
                    # model on THIS endpoint is usable.
                    _u = str(_row.get("base_url") or "").strip().rstrip("/")
                    if _u and _u != (base or "").rstrip("/"):
                        continue
                    sub = str(_row["model"]).strip()
                    break
        except Exception:  # noqa: BLE001 — the registry is optional
            sub = ""
        if not sub and _missing:
            try:
                from aiforge_core.llm.client._models import (
                    model_is_missing, pick_substitute)
                # WITH the key: /v1/models is authenticated on most hosted
                # endpoints, and an unauthenticated probe 401s, returns "no
                # answer", and the rescue silently never fires — team mode
                # dying on the exact config line chat recovers from.
                served = model_is_missing(base, _mid, _api_key_of(model))
                sub = pick_substitute(_mid, served or [])
            except Exception:  # noqa: BLE001 — a rescue must never add a failure
                return
        if not sub:
            return
        log.warning(
            "llm.model_substituted role=%s attempt=%s configured=%s using=%s "
            "api_base=%s — the configured model is not served here; fix the "
            "role config or load it", self.role, label,
            getattr(model, "model", "?"), sub, base)
        # PER-CALL, via the caller's dict. Stored on the instance it would be
        # cross-attributed the moment two calls share this EscalatingLlm (it is
        # built once per role per ticket), billing one call's tokens to the
        # other's model.
        meta["model"] = sub
        try:
            await _throttle_global()
            _tok = _meter_record(self.role, sub)
            meta["token"] = _tok
            out = []
            async for r in model.generate_content_async(
                    req.model_copy(update={"model": sub}), stream=False):
                out.append(r)
        except Exception as sub_exc:  # noqa: BLE001
            _meter_fail(_tok, sub_exc)
            log.warning("llm.model_substitute_failed role=%s err=%.200s",
                        self.role, str(sub_exc))
            return
        if not out or all(_is_empty(r) for r in out):
            _meter_fail(_tok, reason="empty")
            return
        for r in out:
            yield r

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        # Streaming path: trust primary, no retry magic.
        if stream:
            assert self.primary_model is not None
            try:
                await _throttle_global()
            except Exception:  # noqa: BLE001 — nothing here may break a stream
                pass
            _tok = _meter_record(
                self.role, getattr(self.primary_model, "model", None))
            # Track CONTENT, not chunk count: `_is_empty` strips <think>
            # blocks, so a reasoning model that streams a think-only reply
            # yields plenty of chunks and answers nothing. Counting chunks let
            # exactly that — the local-model failure this codebase documents as
            # the common one — read healthy on the streaming path while the
            # non-streaming path called the identical reply `empty`.
            _answered = False
            try:
                async for r in self.primary_model.generate_content_async(
                    llm_request, stream=True,
                ):
                    if not _answered and not _is_empty(r):
                        _answered = True
                    yield r
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
                # NOT bare BaseException: a consumer that stops iterating
                # throws GeneratorExit in here, and abandoning a stream the
                # model answered fine is not a failed request.
                _meter_fail(_tok, exc)
                raise
            if not _answered:
                # A stream that ends having yielded nothing is the same
                # outcome the non-streaming path calls `empty` — counting it
                # as a success would let a wedged model look healthy on the
                # one path with no retry behind it.
                _meter_fail(_tok, reason="empty")
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
                _tok = None
                for _t in range(_tries):
                    try:
                        buffered = []
                        await _throttle_global()
                        _tok = _meter_record(self.role, target_model)
                        async for r in model.generate_content_async(
                            req_for_attempt, stream=False,
                        ):
                            buffered.append(r)
                        break
                    except Exception as _ie:  # noqa: BLE001
                        # Every try is its own counted request, so every try
                        # that dies is its own counted failure — including the
                        # ones this loop swallows by retrying, which are
                        # precisely the invisible calls the meter exists for.
                        _meter_fail(_tok, _ie)
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
                # The model id is wrong, not the box. Same rescue the direct
                # client path does, mirrored here because ADK agents never
                # touch llm.client — without it team mode is the one path that
                # still dies on a stale line of config while chat recovers.
                # PRIMARY only, like the LM-crash recovery below and like the
                # client-side rescue, which only ever substitutes the primary.
                # A cloud candidate's 404 for a decommissioned id must not be
                # silently re-issued against whatever a proxy happens to serve:
                # that is a billed generation on a model nobody chose.
                _sub_out = None
                _sub_meta: dict = {}
                if label in ("primary", "primary_retry"):
                    async for _r in self._substitute_model(
                            exc, model, req_for_attempt, label, _sub_meta):
                        if _sub_out is None:
                            _sub_out = []
                        _sub_out.append(_r)
                _sub_used = _sub_meta.get("model")
                if _sub_out:
                    # The stand-in produced the answer, so the accounting names
                    # IT: tokens, budget and the Langfuse trace all used to
                    # short-circuit here, leaving a rescued team run counted as
                    # a request with zero tokens and traced against the model
                    # that generated nothing.
                    _in_t, _out_t = _usage_of(_sub_out)
                    if _in_t or _out_t:
                        _meter_tokens(self.role, _in_t, _out_t,
                                      _sub_meta.get("token"))
                        try:
                            from aiforge_core.runtime.budget import tracker
                            tracker.record(role=self.role,
                                           model=_sub_used or label,
                                           input_tokens=_in_t,
                                           output_tokens=_out_t)
                        except Exception as _bexc:  # noqa: BLE001
                            log.debug("budget.record failed: %s", _bexc)
                    # A rescue that worked clears the demotion the failure would
                    # otherwise leave behind — else every later call re-walks
                    # the whole cloud chain before reaching the same rescue.
                    if label in ("primary", "primary_retry"):
                        self.primary_demoted = False
                    _mirror_to_langfuse(
                        self.role, req_for_attempt, _sub_out,
                        _sub_used or getattr(model, "model", "") or label,
                        int((_time.monotonic() - _t0) * 1000))
                    for _r in _sub_out:
                        yield _r
                    return
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
                            _rtok = None
                            try:
                                # Gated like the two paths above: a recovery
                                # retry is a real request to the model and must
                                # be both throttled and counted.
                                await _throttle_global()
                                _rtok = _meter_record(self.role, target_model)
                                async for r in model.generate_content_async(
                                    req_for_attempt, stream=False,
                                ):
                                    buffered.append(r)
                            except Exception as retry_exc:  # noqa: BLE001
                                _meter_fail(_rtok, retry_exc)
                                last_exc = retry_exc
                                log.warning(
                                    "llm.recovery_retry_failed role=%s "
                                    "err=%s", self.role,
                                    str(retry_exc)[:200],
                                )
                                if label == "primary":
                                    self._record_primary_failure()
                                continue
                            if not buffered or all(_is_empty(r) for r in buffered):
                                # Counted, answered nothing — the same failure
                                # the `attempt_empty` branch below records for
                                # the normal path.
                                _meter_fail(_rtok, reason="empty")
                            else:
                                # A recovered response is a real one: count what
                                # it WROTE and record its spend. This branch
                                # yielded and returned before ever reaching the
                                # accounting block, so a crash-and-recover box
                                # reported traffic with no tokens behind it.
                                _rin, _rout = _usage_of(buffered)
                                if _rin or _rout:
                                    _meter_tokens(self.role, _rin, _rout, _rtok)
                                    try:
                                        from aiforge_core.runtime.budget import (
                                            tracker as _tr)
                                        _tr.record(role=self.role,
                                                   model=target_model or label,
                                                   input_tokens=_rin,
                                                   output_tokens=_rout)
                                    except Exception as _bx:  # noqa: BLE001
                                        log.debug("budget.record failed: %s", _bx)
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
                # A counted request that answered nothing. The pipeline treats
                # it as a failed attempt (it demotes on it and escalates to the
                # next candidate) and so must the meter — an endpoint returning
                # empties looks perfectly healthy on a success-blind rate.
                _meter_fail(_tok, reason="empty")
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
                in_t, out_t = _usage_of(buffered)
                if in_t or out_t:
                    tracker.record(
                        role=self.role,
                        model=getattr(model, "model", "") or label,
                        input_tokens=in_t, output_tokens=out_t,
                    )
                    # …and into the live meter, mirrored here for the same
                    # reason the request count and the failure count are: this
                    # path never touches llm.client, and a token meter blind to
                    # team mode is blind to the highest-volume writer in the
                    # system.
                    _meter_tokens(self.role, in_t, out_t, _tok)
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
