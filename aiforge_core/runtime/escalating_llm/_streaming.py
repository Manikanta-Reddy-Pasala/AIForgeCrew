"""Streaming through EscalatingLlm: when a stream may be retried and how a
streamed primary call settles."""
from __future__ import annotations

import asyncio

from google.adk.models.llm_request import LlmRequest

from ._policy import (
    _attempt_retries,
    _is_transient_llm_error,
)
from ._quieting import log


def _pkg():
    """``_wrapper``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``_wrapper``; patch any other
    name on this module."""
    import aiforge_core.runtime.escalating_llm._wrapper as package
    return package


class _StreamMixin:
    """Streaming helpers for :class:`EscalatingLlm`."""

    def _stream_retry_ok(self, emitted: bool, attempt: int, tries: int,
                         exc: BaseException) -> bool:
        """Whether to try the SAME endpoint again after a failed stream.

        A second try is only honest while NOTHING has been emitted — once text
        is out, the consumer cannot be handed a second beginning. The fallback
        chain stays unwalked for that same reason.
        """
        return (not emitted and attempt + 1 < tries
                and _is_transient_llm_error(exc))

    def _settle_stream(self, tok, target, answered: bool,
                       buffered: list) -> None:
        """Account for a stream that ended without raising."""
        if not answered:
            # A stream that ends having yielded nothing is the same outcome
            # the non-streaming path calls `empty` — counting it as a success
            # would let a wedged model look healthy.
            _pkg()._meter_fail(tok, reason="empty")
        elif buffered:
            # Spend was recorded on the buffered path and not here, so a
            # streamed turn's tokens were invisible in the budget.
            self._record_spend(target or "primary", buffered, tok)

    async def _stream_primary(self, llm_request: LlmRequest):
        """The streaming path: primary only — but no longer bare.

        The fallback CHAIN stays deliberately unwalked: re-emitting partial
        chunks from a second provider mid-answer would violate the streaming
        contract (a consumer that has already seen text cannot be handed a
        second beginning). What this now carries, because none of it re-emits
        anything, is the rest of what the buffered path had: the per-model
        request STAMPING, a bounded retry on the SAME endpoint while nothing
        has been emitted yet, and SPEND RECORDING on success. Without those, a
        single transient 5xx ended a team agent outright — which is the reason
        team streaming was opt-in, and why answers arrived in one lump.
        """
        pkg = _pkg()
        assert self.primary_model is not None
        model = self.primary_model
        target = getattr(model, "model", None)
        req = self._stamp_request(llm_request, model)
        tries = _attempt_retries()
        for attempt in range(tries):
            emitted = False          # has the consumer seen ANY chunk yet?
            answered = False         # has it seen any real CONTENT?
            buffered: list = []
            try:
                await pkg._throttle_global(self.role)
            except Exception:  # noqa: BLE001 — nothing here may break a stream
                pass
            tok = pkg._meter_record(self.role, target)
            try:
                async for r in model.generate_content_async(req, stream=True):
                    emitted = True
                    # Track CONTENT, not chunk count: `_is_empty` strips
                    # <think> blocks, so a reasoning model that streams a
                    # think-only reply yields plenty of chunks and answers
                    # nothing. Counting chunks let exactly that — the
                    # local-model failure this codebase documents as the common
                    # one — read healthy here while the non-streaming path
                    # called the identical reply `empty`.
                    if not answered and not pkg._is_empty(r):
                        answered = True
                    buffered.append(r)
                    yield r
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
                # NOT bare BaseException: a consumer that stops iterating throws
                # GeneratorExit in here, and abandoning a stream the model
                # answered fine is not a failed request.
                pkg._meter_fail(tok, exc)
                if self._stream_retry_ok(emitted, attempt, tries, exc):
                    log.warning("llm.stream_retry role=%s try=%d/%d err=%.140s",
                                self.role, attempt + 1, tries, str(exc))
                    continue
                raise
            self._settle_stream(tok, target, answered, buffered)
            return
