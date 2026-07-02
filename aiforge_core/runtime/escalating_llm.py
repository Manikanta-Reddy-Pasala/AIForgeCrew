"""Self-healing ADK ``BaseLlm`` wrapper with cloud escalation.

Wraps a primary ADK model (LiteLlm against the operator's local mlx-lm)
and an ordered list of cloud fallbacks
(Ollama Cloud). On failure of the
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

import asyncio
import json
import logging
import os
from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse


log = logging.getLogger("aiforge.escalating_llm")


def _repair_json(s: str) -> str:
    """Best-effort repair of truncated / malformed tool-call argument JSON.

    Weaker local models (and output-token truncation) emit tool calls whose
    ``arguments`` JSON is cut mid-string — ``json.loads`` then raises
    ``Unterminated string`` and ADK aborts the whole run. Close the open
    string + balance brackets so the call at least parses; fall back to
    ``{}`` if still unparseable (the tool then reports a clear error and the
    agent retries) rather than killing the pipeline.
    """
    if not s or not s.strip():
        return "{}"
    try:
        json.loads(s)
        return s
    except Exception:  # noqa: BLE001
        pass
    t = s.strip()
    # Walk the string tracking string-state + bracket stack.
    stack: list[str] = []
    in_str = False
    esc = False
    closing = {"{": "}", "[": "]"}
    for ch in t:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(closing[ch])
            elif ch in "}]" and stack and stack[-1] == ch:
                stack.pop()
    if in_str:
        t += '"'        # close the unterminated string
    while stack:
        t += stack.pop()  # close open objects/arrays
    try:
        json.loads(t)
        return t
    except Exception:  # noqa: BLE001
        return "{}"


def _install_adk_toolarg_repair() -> None:
    """Patch ADK's lite_llm so a malformed tool-call ``arguments`` JSON is
    repaired instead of crashing the run (idempotent)."""
    try:
        import google.adk.models.lite_llm as _ll
    except Exception:  # noqa: BLE001
        return
    if getattr(_ll, "_aiforge_toolarg_patched", False):
        return
    _orig = _ll._message_to_generate_content_response
    def _patched(message, *a, **k):  # noqa: ANN001
        try:
            return _orig(message, *a, **k)
        except json.JSONDecodeError:
            for tc in (getattr(message, "tool_calls", None) or []):
                fn = getattr(tc, "function", None)
                if fn is not None and getattr(fn, "arguments", None):
                    fixed = _repair_json(fn.arguments)
                    log.warning("repaired malformed tool-call args: %.60s… → %s",
                                fn.arguments, fixed[:80])
                    fn.arguments = fixed
            return _orig(message, *a, **k)
    _ll._message_to_generate_content_response = _patched
    _ll._aiforge_toolarg_patched = True
    _quiet_litellm()
    _quiet_adk_tracebacks()


def _quiet_litellm() -> None:
    """Silence litellm's noisy internal logging worker (the async callback
    queue spams ERROR/Task-never-retrieved tracebacks when the run loop is
    cancelled — e.g. on Stop). Idempotent, best-effort."""
    try:
        import litellm as _l
        _l.suppress_debug_info = True
        _l.set_verbose = False
        _l.success_callback = []
        _l.failure_callback = []
        _l._async_success_callback = []
        _l._async_failure_callback = []
        for name in ("LiteLLM", "litellm", "LiteLLM Router", "LiteLLM Proxy"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.CRITICAL)
            lg.propagate = False
        try:
            from litellm._logging import verbose_logger as _vl
            _vl.setLevel(logging.CRITICAL)
            _vl.propagate = False
        except Exception:  # noqa: BLE001
            pass
        # FULLY neutralise the async LoggingWorker. It spawns a persistent
        # background task bound to whatever event loop is running; the team
        # pipeline creates a NEW loop per run and closes it, orphaning the
        # worker → "Task was destroyed but it is pending" / "Event loop is
        # closed" / "task_done() too many times" spam. No-op every entry
        # point so it never starts a task or processes one (we don't use
        # litellm's async callbacks anyway).
        try:
            from litellm.litellm_core_utils import logging_worker as _lw

            def _drop(self, async_coroutine=None, *a, **k):  # noqa: ANN001
                try:
                    if async_coroutine is not None:
                        async_coroutine.close()
                except Exception:  # noqa: BLE001
                    pass

            async def _anoop(self, *a, **k):  # noqa: ANN001
                return None

            _lw.LoggingWorker.ensure_initialized_and_enqueue = _drop
            _lw.LoggingWorker.enqueue = _drop
            _lw.LoggingWorker.start = lambda self, *a, **k: None
            _lw.LoggingWorker.flush = _anoop
            _lw.LoggingWorker._flush_on_exit = lambda self, *a, **k: None
            gw = getattr(_lw, "GLOBAL_LOGGING_WORKER", None)
            if gw is not None:
                try:
                    if getattr(gw, "_worker_task", None) is not None:
                        gw._worker_task.cancel()
                    gw._worker_task = None
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


class _StripTracebackFilter(logging.Filter):
    """Keep a record's one-line message but drop its multi-page traceback.

    ADK's ``_node_runner`` calls ``logger.exception("Node execution failed
    with exception")`` for EVERY node error — dumping the full chained
    litellm/httpx stack into the console even though the failure is already
    captured (a) in the ADK error_event and (b) by our own concise
    ``llm.exhausted`` ERROR line. The raw stack is pure noise to the operator,
    so we strip ``exc_info`` and let the meaningful one-liners stand.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.exc_info = None
        record.exc_text = None
        return True


def _quiet_adk_tracebacks() -> None:
    """Silence ADK's node-runner full-traceback dumps (idempotent).

    Operator sees the concise ``llm.exhausted`` / ``llm.attempt_failed`` lines
    instead of a chained httpx→openai→litellm stack for every flaky call.
    Override with ``AIFORGE_ADK_TRACEBACKS=1`` to restore the raw dumps when
    debugging a genuinely novel node failure.
    """
    if _truthy_env("AIFORGE_ADK_TRACEBACKS"):
        return
    # Every ADK logger that dumps a full chained stack on a recoverable /
    # already-surfaced failure: the workflow node runner, the top-level
    # runner ("Root node X failed.", exc_info=True), and the tool-call flow
    # ("Error in event_id ...", logger.exception). Each filter runs at emit
    # time on its originating logger, so the one-line message survives and
    # only the redundant traceback is stripped.
    for name in ("google_adk.google.adk.workflow._node_runner",
                 "google_adk.google.adk.runners",
                 "google_adk.google.adk.flows.llm_flows.functions"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, _StripTracebackFilter) for f in lg.filters):
            lg.addFilter(_StripTracebackFilter())


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


# Apply at import — before ANY litellm call / team run — so the worker is
# never even created (not just lazily when the first LiteLlm is built).
_quiet_litellm()
_quiet_adk_tracebacks()


# Substrings that mark a TRANSIENT failure worth retrying the SAME endpoint
# with backoff. Includes 401/403 on purpose — the self-hosted proxy
# (nginx) returns intermittent "401 Authorization Required" even with a
# valid token; bounded retries ride over the blip instead of surfacing an
# "agent error" in the UI. Truly-bad creds still stop after the cap.
_TRANSIENT_MARKERS = (
    "authenticationerror", "401", "403", "authorization required",
    "timeout", "timedout", "timed out", "connection", "econnreset", "reset",
    "temporarily", "unavailable", "rate limit", "ratelimit", "429",
    "500", "502", "503", "504", "bad gateway", "gateway", "overloaded",
    "internalservererror", "apiconnectionerror", "serviceunavailable",
    "jsondecodeerror", "unterminated", "remotedisconnected", "broken pipe",
)


def _attempt_retries() -> int:
    # Default 1 (one try, then escalate to the next candidate in the chain).
    # For a LOCAL primary, 3× same-endpoint read-retries on a transient error
    # just burns serial minutes before reaching the cloud rescue — the
    # connect-preflight already fails-fast on unreachable hosts, so these are
    # pure read-retry latency. Env override preserved for ops.
    try:
        return max(1, int(os.environ.get("AIFORGE_LLM_ATTEMPT_RETRIES", "1")))
    except ValueError:
        return 1


def _is_transient_llm_error(exc: Exception) -> bool:
    s = (type(exc).__name__ + " " + str(exc)).lower()
    return any(m in s for m in _TRANSIENT_MARKERS)


def _api_base_of(model: Any) -> str:
    """Best-effort endpoint URL for a built model. ADK's LiteLlm doesn't expose
    ``api_base`` directly — it lives in ``_additional_args`` — so ``getattr``
    alone logged ``?`` and the LM-crash recovery couldn't find the endpoint."""
    base = getattr(model, "api_base", None)
    if not base:
        extra = getattr(model, "_additional_args", None)
        if isinstance(extra, dict):
            base = extra.get("api_base")
    return base or ""


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

    * ``custom_llm_provider`` — override LiteLLM's URL/model auto-detect.
      Required for ollama.com (OpenAI-compat at ``/v1`` but LiteLLM
      misroutes to ``/api/generate`` without it).
    """
    from google.adk.models.lite_llm import LiteLlm
    _install_adk_toolarg_repair()
    kwargs: dict[str, Any] = {"model": cfg["model_id"]}
    # Generous output budget so a tool call carrying file content isn't
    # truncated mid-string (→ malformed JSON args). Tunable; some endpoints
    # cap it, so keep it overridable.
    # Operator-tunable generation cap (UI → runtime_settings.json → env →
    # default). Too small truncates a doer's file-write tool-call args.
    try:
        from aiforge_core.config import runtime_settings as _rs
        kwargs["max_tokens"] = _rs.get("max_output_tokens")
    except Exception:  # noqa: BLE001 — never block a build on settings
        import os as _os_mt
        try:
            kwargs["max_tokens"] = int(
                _os_mt.environ.get("AIFORGE_LLM_MAX_TOKENS", "32768"))
        except ValueError:
            kwargs["max_tokens"] = 32768
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
            # litellm's HTTP client reads the GLOBAL `litellm.ssl_verify`
            # when it builds (and caches) its aiohttp/httpx connector — the
            # per-call ssl_verify kwarg above does NOT reconfigure an
            # already-built connector, so a self-signed internal endpoint
            # still raised CERTIFICATE_VERIFY_FAILED. Set the global here
            # (pipeline-build time, before the first completion) so the
            # connector is built with verification off. Also force httpx
            # (disable the aiohttp transport) where ssl_verify is honoured
            # most predictably. NOTE: this relaxes verification for litellm
            # globally in this process — acceptable for a self-hosted deploy
            # whose model endpoint uses an internal/self-signed cert.
            try:
                import litellm as _ll
                if _ll.ssl_verify is not False:
                    _ll.ssl_verify = False
                    _ll.disable_aiohttp_transport = True
                    import os as _o
                    _o.environ.setdefault("SSL_VERIFY", "False")
                    log.warning(
                        "litellm TLS verification disabled (insecure/internal "
                        "model endpoint %s) — set AIFORGE_LLM_CA_BUNDLE to a "
                        "PEM to keep verification on.", api_base)
            except Exception:  # noqa: BLE001
                pass
    # Match the urllib client path: a generous request timeout (self-hosted
    # reasoning models need minutes) and a non-default User-Agent (some
    # proxies/WAFs reject httpx/litellm's default). Both env-tunable. Applied
    # to the team-flow / ticket pipeline (LiteLLM) so it agrees with simple
    # chat (client._post).
    import os as _os
    try:
        _read_to = float(_os.environ.get("AIFORGE_LLM_TIMEOUT_S", "600"))
    except ValueError:
        _read_to = 600.0
    # Split connect from read. A scalar timeout applies to BOTH — so an
    # unreachable/asleep host (dropped SYN, no RST) blocks the full read
    # timeout (600s) just to fail the TCP connect, and with 3 attempt-retries
    # × the candidate chain × node-level RetryConfig that compounds into a
    # multi-HOUR retry storm that freezes the single-shot ticket runner (the
    # "pipeline runs forever" symptom). A short CONNECT timeout fails an
    # unreachable endpoint in seconds so escalation moves on immediately,
    # while the generous READ timeout still lets a live reasoning model think
    # for minutes. litellm forwards httpx.Timeout natively.
    try:
        _connect_to = float(_os.environ.get("AIFORGE_LLM_CONNECT_TIMEOUT_S", "8"))
    except ValueError:
        _connect_to = 8.0
    try:
        import httpx as _httpx
        kwargs["timeout"] = _httpx.Timeout(
            _read_to, connect=min(_connect_to, _read_to))
    except Exception:  # noqa: BLE001 — fall back to the scalar if httpx absent
        kwargs["timeout"] = _read_to
    kwargs["extra_headers"] = {
        "User-Agent": _os.environ.get("AIFORGE_LLM_USER_AGENT", "curl/8.5.0 (aiforge)"),
    }
    # ADK's LiteLlm hardcodes ``stream_options={"include_usage": True}`` on
    # every streaming completion. Strict OpenAI-compatible proxies (e.g. a
    # self-hosted gateway that buffers and drops ``stream:true``) then reject
    # the request with "Stream options can only be defined when stream=True".
    # stream_options only carries usage-in-stream accounting, so drop it at
    # the litellm layer for all attempts. ``drop_params`` additionally lets
    # litellm silently shed any other param the endpoint doesn't accept.
    kwargs["drop_params"] = True
    kwargs["additional_drop_params"] = ["stream_options"]
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
                    from . import local_starter
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
