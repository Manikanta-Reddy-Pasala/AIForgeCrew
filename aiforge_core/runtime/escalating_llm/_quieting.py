"""Telemetry/logging silencing + tool-call JSON repair (leaf helpers).

Split out of the former single-module ``escalating_llm``; behaviour identical.
Shared ``log`` logger lives here so every submodule imports the SAME instance.
"""
from __future__ import annotations

import json
import logging
import os


log = logging.getLogger("aiforge.escalating_llm")


def _unclosed(text: str) -> tuple[bool, list[str]]:
    """``(inside a string, the closers still owed)`` after walking ``text``."""
    stack: list[str] = []
    in_str = False
    esc = False
    closing = {"{": "}", "[": "]"}
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(closing[ch])
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    return in_str, stack


def _parses(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except Exception:  # noqa: BLE001
        return False


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
    if _parses(s):
        return s
    t = s.strip()
    in_str, stack = _unclosed(t)
    if in_str:
        t += '"'                      # close the unterminated string
    while stack:
        t += stack.pop()              # close open objects/arrays
    return t if _parses(t) else "{}"


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


def _disable_litellm_callbacks(_l) -> None:
    """Kill LiteLLM's phone-home telemetry (defaults True → posts anonymous
    usage to PostHog) and every callback list. Network+telemetry lockdown: no
    unsolicited egress."""
    try:
        _l.telemetry = False
    except Exception:  # noqa: BLE001
        pass
    _l.suppress_debug_info = True
    _l.set_verbose = False
    _l.success_callback = []
    _l.failure_callback = []
    _l._async_success_callback = []
    _l._async_failure_callback = []


def _silence_litellm_loggers() -> None:
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


def _neutralise_logging_worker() -> None:
    """FULLY neutralise the async LoggingWorker.

    It spawns a persistent background task bound to whatever event loop is
    running; the team pipeline creates a NEW loop per run and closes it,
    orphaning the worker → "Task was destroyed but it is pending" / "Event loop
    is closed" / "task_done() too many times" spam. Every entry point becomes a
    no-op so it never starts a task or processes one — we don't use litellm's
    async callbacks anyway.
    """
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
        if gw is None:
            return
        try:
            if getattr(gw, "_worker_task", None) is not None:
                gw._worker_task.cancel()
            gw._worker_task = None
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _quiet_litellm() -> None:
    """Silence litellm's noisy internal logging worker (the async callback
    queue spams ERROR/Task-never-retrieved tracebacks when the run loop is
    cancelled — e.g. on Stop). Idempotent, best-effort."""
    try:
        import litellm as _l
        _disable_litellm_callbacks(_l)
        _silence_litellm_loggers()
        _neutralise_logging_worker()
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
