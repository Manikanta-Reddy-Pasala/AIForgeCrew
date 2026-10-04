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


def _thought_text(resp) -> str:
    """The reasoning in one streamed chunk."""
    parts = getattr(getattr(resp, "content", None), "parts", None) or []
    return "".join(getattr(p, "text", "") or "" for p in parts
                   if getattr(p, "thought", None) is True)


#: Said after the notes when a cut reasoning phase is asked for its answer.
WRAP_UP = ("[Your thinking time is up. The notes above are your own reasoning "
           "so far. Do not think further: write the final answer now, complete, "
           "in exactly the format your instructions require.]")


def _has_answer(resp) -> bool:
    """Real content: text that is not reasoning, or a tool call."""
    if _pkg()._is_empty(resp):
        return False
    parts = getattr(getattr(resp, "content", None), "parts", None) or []
    return any(getattr(p, "thought", None) is not True
               and (getattr(p, "text", None) or getattr(p, "function_call", None))
               for p in parts)


def _reasoning_budget_chars() -> int:
    """The reasoning budget as characters of streamed thought (~4 per token)."""
    try:
        from aiforge_core.llm import reasoning
        return reasoning.budget_tokens() * 4
    except Exception:  # noqa: BLE001
        return 0


class _StreamMixin:
    """Streaming helpers for :class:`EscalatingLlm`."""

    def _plain_retry(self, llm_request: LlmRequest, notes: str = ""):
        """``(model, request)`` for asking again WITHOUT reasoning. The
        reasoning done so far is not thrown away: it goes back in as the
        model's own notes, and the answer is written from them."""
        from aiforge_core.llm import reasoning
        model = self.plain_model
        req = self._stamp_request(llm_request, model)
        if notes.strip():
            from google.genai import types
            req = req.model_copy(update={"contents": [
                *(req.contents or []),
                types.Content(role="model", parts=[
                    types.Part.from_text(text="My notes so far:\n" + notes.strip())]),
                types.Content(role="user", parts=[
                    types.Part.from_text(text=WRAP_UP)])]})
        return model, reasoning.no_think_request(req)

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
        waiter = None
        attempt = -1
        # A role that reasons has a twin with reasoning off. Its reasoning is
        # BOUNDED: past the budget — or when the stream ends with thoughts and
        # no answer (the reply cap ran out mid-thought) — the same request is
        # asked again without reasoning, once, so the stage always answers.
        # The finished (non-partial) responses are held back until then: a
        # think-only one must not reach the agent as its reply.
        budget = (_reasoning_budget_chars()
                  if getattr(self, "plain_model", None) is not None else 0)
        while True:
            attempt += 1
            emitted = False          # has the consumer seen ANY chunk yet?
            answered = False         # has it seen any real CONTENT?
            buffered: list = []
            held: list = []          # finished responses, while the guard is on
            notes: list = []         # the reasoning streamed so far
            thought, overrun = 0, False
            try:
                await pkg._throttle_global(self.role)
            except Exception:  # noqa: BLE001 — nothing here may break a stream
                pass
            tok = pkg._meter_record(self.role, target)
            try:
                stream = model.generate_content_async(req, stream=True)
                async for r in stream:
                    emitted = True
                    # Track CONTENT, not chunk count: `_is_empty` strips
                    # <think> blocks, so a reasoning model that streams a
                    # think-only reply yields plenty of chunks and answers
                    # nothing. Counting chunks let exactly that — the
                    # local-model failure this codebase documents as the common
                    # one — read healthy here while the non-streaming path
                    # called the identical reply `empty`.
                    if not answered and not pkg._is_empty(r) and (
                            not budget or _has_answer(r)):
                        answered = True
                    buffered.append(r)
                    if not budget:
                        yield r
                        continue
                    if not getattr(r, "partial", False):
                        held.append(r)
                        continue
                    if not answered:
                        notes.append(_thought_text(r))
                        thought += len(notes[-1])
                        if thought > budget:
                            overrun = True
                            break
                    yield r
                if overrun:
                    try:                # stop the server generating
                        await stream.aclose()
                    except Exception:  # noqa: BLE001
                        pass
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
                # NOT bare BaseException: a consumer that stops iterating throws
                # GeneratorExit in here, and abandoning a stream the model
                # answered fine is not a failed request.
                pkg._meter_fail(tok, exc)
                if self._stream_retry_ok(emitted, attempt, tries, exc):
                    log.warning("llm.stream_retry role=%s try=%d/%d err=%.140s",
                                self.role, attempt + 1, tries, str(exc))
                    continue
                # Nothing emitted yet and the model is DOWN: wait for it, then
                # start the stream again (a fresh set of tries).
                if emitted or not isinstance(exc, Exception):
                    raise
                waiter = waiter or self._outage_waiter()
                if not waiter.waitable(exc):
                    raise
                await waiter.await_wait(exc)
                attempt = -1
                continue
            if waiter is not None:
                waiter.recovered()
            if budget and not answered:
                log.warning("llm.reasoning_cut role=%s reason=%s thought_chars=%d "
                            "budget_chars=%d — asking again without reasoning",
                            self.role, "over_budget" if overrun else "no_answer",
                            thought, budget)
                pkg._meter_fail(tok, reason="reasoning_overrun")
                # the newest reasoning is the most settled: keep its tail
                model, req = self._plain_retry(
                    llm_request, "".join(notes)[-budget:])
                target = getattr(model, "model", None)
                budget, attempt = 0, -1
                continue
            for r in held:
                yield r
            self._settle_stream(tok, target, answered, buffered)
            return
