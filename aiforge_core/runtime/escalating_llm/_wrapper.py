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

    @staticmethod
    def _substitution_allowed(exc) -> bool:
        """Whether a stand-in is warranted at all.

        Trigger differs by source: a MISSING model is a config error and every
        candidate is worth trying, while a model that is served but not
        answering only justifies the registry chain.

        The operator's kill switch applies HERE too. The direct-client rescue is
        gated on it and documents why: someone comparing models wants a wrong id
        to be a hard failure. Honouring it in chat and ignoring it in team mode
        is the same silent substitution the flag exists to prevent, on the path
        that runs a whole ticket.
        """
        if not (_looks_like_missing_model(exc) or _is_transient_llm_error(exc)):
            return False
        try:
            from aiforge_core.llm.client import _autofallback_enabled
            return bool(_autofallback_enabled())
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    def _registry_substitute(mid: str, base: str) -> str:
        """The operator's next configured model ON THIS ENDPOINT, or "".

        The same chain the chat path walks, so "I added four models, use the
        others when one dies" means the same thing in team mode. Registry rows
        that name ANOTHER host are for the text path, which can rebuild the
        endpoint; here the request is bound to this agent's own client, so only
        a different model on this endpoint is usable.
        """
        try:
            from aiforge_core.config import model_registry as _mr
            rows = _mr.chain_after(mid, base)
        except Exception:  # noqa: BLE001 — the registry is optional
            return ""
        want = (base or "").rstrip("/")
        for row in rows:
            if not (isinstance(row, dict) and str(row.get("model") or "").strip()):
                continue
            url = str(row.get("base_url") or "").strip().rstrip("/")
            if url and url != want:
                continue
            return str(row["model"]).strip()
        return ""

    @staticmethod
    def _served_substitute(model, mid: str, base: str) -> str | None:
        """A model this endpoint reports as loaded — the only option when the
        failure is "that model is not loaded here". None means the probe itself
        failed, and a rescue must never add a failure.

        Probed WITH the key: /v1/models is authenticated on most hosted
        endpoints, and an unauthenticated probe 401s, returns "no answer", and
        the rescue silently never fires — team mode dying on the exact config
        line chat recovers from.
        """
        try:
            from aiforge_core.llm.client._models import (
                model_is_missing, pick_substitute)
            served = model_is_missing(base, mid, _api_key_of(model))
            return pick_substitute(mid, served or [])
        except Exception:  # noqa: BLE001
            return None

    def _substitute_id(self, exc, model) -> str:
        """The model id to stand in with, or "" for none. Registry first, then
        (for a missing model only) whatever the endpoint says it serves."""
        base = _api_base_of(model)
        if not base:
            return ""
        mid = getattr(model, "model", "") or ""
        sub = self._registry_substitute(mid, base)
        if sub or not _looks_like_missing_model(exc):
            return sub
        return self._served_substitute(model, mid, base) or ""

    async def _substitute_model(self, exc, model, req, label, meta: dict):
        """Re-issue ONE attempt against a model this endpoint actually serves.

        Yields the responses when the stand-in worked and nothing when it did
        not — the caller then falls through to the cloud chain exactly as
        before. LiteLlm picks ``llm_request.model`` before its own, so the
        substitution is a stamped request, not a rebuilt model object.
        """
        if not self._substitution_allowed(exc):
            return
        sub = self._substitute_id(exc, model)
        if not sub:
            return
        log.warning(
            "llm.model_substituted role=%s attempt=%s configured=%s using=%s "
            "api_base=%s — the configured model is not served here; fix the "
            "role config or load it", self.role, label,
            getattr(model, "model", "?"), sub, _api_base_of(model))
        # PER-CALL, via the caller's dict. Stored on the instance it would be
        # cross-attributed the moment two calls share this EscalatingLlm (it is
        # built once per role per ticket), billing one call's tokens to the
        # other's model.
        meta["model"] = sub
        _tok = None
        try:
            await _throttle_global(self.role)
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

    async def _stream_primary(self, llm_request: LlmRequest):
        """The streaming path: trust primary, no retry magic."""
        assert self.primary_model is not None
        try:
            await _throttle_global(self.role)
        except Exception:  # noqa: BLE001 — nothing here may break a stream
            pass
        tok = _meter_record(self.role,
                            getattr(self.primary_model, "model", None))
        # Track CONTENT, not chunk count: `_is_empty` strips <think> blocks, so
        # a reasoning model that streams a think-only reply yields plenty of
        # chunks and answers nothing. Counting chunks let exactly that — the
        # local-model failure this codebase documents as the common one — read
        # healthy on the streaming path while the non-streaming path called the
        # identical reply `empty`.
        answered = False
        try:
            async for r in self.primary_model.generate_content_async(
                    llm_request, stream=True):
                if not answered and not _is_empty(r):
                    answered = True
                yield r
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
            # NOT bare BaseException: a consumer that stops iterating throws
            # GeneratorExit in here, and abandoning a stream the model answered
            # fine is not a failed request.
            _meter_fail(tok, exc)
            raise
        if not answered:
            # A stream that ends having yielded nothing is the same outcome the
            # non-streaming path calls `empty` — counting it as a success would
            # let a wedged model look healthy on the one path with no retry
            # behind it.
            _meter_fail(tok, reason="empty")

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
        return model_overrides.apply(target, req, role=self.role)

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

    async def _rescue_by_substitution(self, exc, model, req, label, t0):
        """Yield a stand-in model's answer, or nothing.

        PRIMARY only, like the LM-crash recovery and like the client-side
        rescue, which only ever substitutes the primary. A cloud candidate's 404
        for a decommissioned id must not be silently re-issued against whatever
        a proxy happens to serve: that is a billed generation on a model nobody
        chose.
        """
        if label not in ("primary", "primary_retry"):
            return
        meta: dict = {}
        out: list = []
        async for r in self._substitute_model(exc, model, req, label, meta):
            out.append(r)
        if not out:
            return
        # The stand-in produced the answer, so the accounting names IT: tokens,
        # budget and the Langfuse trace all used to short-circuit here, leaving
        # a rescued team run counted as a request with zero tokens and traced
        # against the model that generated nothing.
        used = meta.get("model")
        self._record_spend(used or label, out, meta.get("token"))
        # A rescue that worked clears the demotion the failure would otherwise
        # leave behind — else every later call re-walks the whole cloud chain
        # before reaching the same rescue.
        self.primary_demoted = False
        _mirror_to_langfuse(self.role, req, out,
                            used or getattr(model, "model", "") or label,
                            int((_time.monotonic() - t0) * 1000))
        for r in out:
            yield r

    def _should_try_lm_reload(self, label: str, err_str: str) -> bool:
        """LM Studio MLX crash mid-pipeline ("model has crashed" / "No models
        loaded"). Without the reload, sticky-demotion locks us off the local
        primary for the rest of the ticket and a stress run starves on cloud
        rate limits. One attempt per pipeline run — a flapping LM Studio would
        otherwise trigger an SSH-load storm."""
        if label not in ("primary", "primary_retry") or self.lm_recovery_tried:
            return False
        from .. import local_starter
        return bool(local_starter.looks_like_lm_crash(err_str))

    async def _rescue_by_lm_reload(self, model, req, label, target, t0,
                                   out: dict):
        """Force-reload the crashed local model and retry the SAME attempt once.
        Yields its answer, or nothing. ``out["exc"]`` carries a retry failure
        back to the caller so the chain reports the freshest error."""
        from .. import local_starter
        self.lm_recovery_tried = True
        recovered = local_starter.try_recover(_api_base_of(model))
        log.warning("llm.lm_crash_recovery role=%s recovered=%s",
                    self.role, recovered)
        if not recovered:
            return
        buffered: list[LlmResponse] = []
        tok = None
        try:
            # Gated like every other send: a recovery retry is a real request to
            # the model and must be both throttled and counted.
            await _throttle_global(self.role)
            tok = _meter_record(self.role, target)
            async for r in model.generate_content_async(req, stream=False):
                buffered.append(r)
        except Exception as exc:  # noqa: BLE001
            _meter_fail(tok, exc)
            out["exc"] = exc
            log.warning("llm.recovery_retry_failed role=%s err=%s",
                        self.role, str(exc)[:200])
            return
        if not buffered or all(_is_empty(r) for r in buffered):
            # Counted, answered nothing — the same failure the `attempt_empty`
            # branch records for the normal path.
            _meter_fail(tok, reason="empty")
            return
        # A recovered response is a real one: count what it WROTE and record its
        # spend. This branch yielded and returned before ever reaching the
        # accounting block, so a crash-and-recover box reported traffic with no
        # tokens behind it.
        self._record_spend(target or label, buffered, tok)
        log.info("llm.recovered role=%s after_lm_reload", self.role)
        _mirror_to_langfuse(self.role, req, buffered,
                            getattr(model, "model", "") or label,
                            int((_time.monotonic() - t0) * 1000))
        for r in buffered:
            yield r

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

    async def _rescue_after_failure(self, exc, model, req, label, target, t0,
                                    state: dict):
        """Both rescue paths for a failed attempt, in order: a stand-in model,
        then an LM-Studio reload. Yields a rescued answer, or nothing — in which
        case the caller moves on to the next candidate."""
        state["exc"] = exc
        async for r in self._rescue_by_substitution(exc, model, req, label, t0):
            state["done"] = True
            yield r
        if state["done"]:
            return
        err_str = str(exc)
        log.warning(
            "llm.attempt_failed role=%s attempt=%s model=%s api_base=%s "
            "errtype=%s err=%s", self.role, label,
            getattr(model, "model", "?"), _api_base_of(model) or "?",
            type(exc).__name__, err_str[:800])
        if self._should_try_lm_reload(label, err_str):
            out: dict = {}
            async for r in self._rescue_by_lm_reload(model, req, label,
                                                     target, t0, out):
                state["done"] = True
                yield r
            if not state["done"] and "exc" in out:
                # The chain reports the freshest error, so a recovery retry that
                # died replaces the crash that triggered it.
                state["exc"] = out["exc"]
        if not state["done"] and label == "primary":
            self._record_primary_failure()

    async def _try_candidate(self, label, model, llm_request, t0, state: dict):
        """One candidate end to end: attempt, then the rescues. Yields the
        responses that answered; yielding nothing means "move to the next
        candidate". ``state`` accumulates the freshest failure and whether the
        call is finished."""
        req = self._stamp_request(llm_request, model)
        target = getattr(model, "model", None)
        meter: dict = {}
        try:
            buffered = await self._attempt(model, req, label, target, meter)
        except Exception as exc:  # noqa: BLE001
            async for r in self._rescue_after_failure(exc, model, req, label,
                                                      target, t0, state):
                yield r
            return

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
