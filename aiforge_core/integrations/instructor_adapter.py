"""instructor adapter — Pydantic-validated structured output over an
OpenAI-compatible endpoint (``pip install aiforgecrew[structured]``).

Uses ``Mode.MD_JSON`` (schema-in-prompt + JSON extraction + auto-reask): the
one mode that works against local servers (LM Studio/MLX) that reject
``response_format: json_object``. Raises on any failure — the domain caller
(:mod:`aiforge_core.llm.structured`) owns the fallback loop.

THIS PATH TALKS TO THE ENDPOINT DIRECTLY. It does not go through
``llm.client._post``, so nothing enforced or counted there applies here unless
it is mirrored — the same trap the ADK/LiteLlm path fell into. Everything
``structured_complete`` reaches is memory-side (compaction folds, scope
classification, graph reconcile, OKF authoring, the session ledger), i.e. the
``learner`` role, which is the busiest unattended sender in the system. Until
this file called ``rate_limiter.acquire_global()`` the operator's
calls-per-minute ceiling did not apply to ANY of it, and the toolbar meter
read zero for it: an operator who set the ceiling below their provider's limit
still collected rate-limit rejections, from traffic they could not see.

Mirrored at the HTTPX TRANSPORT, not at this function's entry, because
instructor retries INSIDE ``create()`` (``max_retries``): a per-call gate would
count one request and send three.
"""
from __future__ import annotations

from pydantic import BaseModel

def _is_rate_limit_body(status: int, body: str) -> bool:
    """Is this response a provider refusing on ITS OWN rate limit?

    ONE definition, imported whole — status half included — from the client's
    classifier. This judgement used to be spelled out here as well, and the two
    copies had already drifted on 401/403 and on whether the body was
    truncated first. Three code paths that must agree about the same sentence
    is three chances for them not to.
    """
    from aiforge_core.llm.client._errors import status_body_is_rate_limited
    return status_body_is_rate_limited(status, body)


def available() -> bool:
    try:
        import instructor  # noqa: F401
        import openai      # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _int_env(name: str, default: int) -> int:
    import os
    try:
        return max(0, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def _wait_budget() -> float:
    """The same ceiling wait budget the wire path uses. Two call sites read it,
    and a bare default in one of them is the same call with two budgets."""
    import os
    try:
        return float(os.environ.get("AIFORGE_LLM_MAX_WAIT_S") or 120)
    except (TypeError, ValueError):
        return 120.0


def _read_error_body(response) -> str:
    """The response body text, best-effort.

    READ FIRST. httpx fires response hooks in _send_handling_redirects, BEFORE
    the body is read — so ``response.text`` raises ResponseNotRead on every real
    call, and only a MockTransport (which hands back a pre-buffered Response)
    makes the other order look correct. Reading is idempotent with httpx's own
    later read.
    """
    try:
        response.read()
        return response.text
    except Exception:  # noqa: BLE001
        try:
            return response.text
        except Exception:  # noqa: BLE001
            return ""


def _note_rate_limit(response, status: int, body: str, provider: str,
                     _rl) -> None:
    """Feed a rate-limit response's Retry-After back into the shared limiter."""
    if not _is_rate_limit_body(status, body):
        return
    try:
        ra = float(response.headers.get("Retry-After") or 0)
    except Exception:  # noqa: BLE001
        ra = 0.0
    try:
        _rl.note_rate_limited(ra, provider=provider)
    except Exception:  # noqa: BLE001
        pass


def _event_hooks(role: str | None, model: str | None, provider: str,
                 pending: list):
    """httpx event hooks that put this path under the SAME ceiling and meter
    as ``llm.client._post``.

    ``request`` runs once per actual send — including instructor's internal
    reasks — which is exactly the unit both the ceiling and the provider count.

    ``pending`` collects the meter token of every send that has not yet been
    settled by a response. A transport error (connection reset, read timeout)
    never reaches the response hook, so without this the failure that hurts
    most would be the one the meter records as a success — see
    :func:`_settle_pending`.
    """
    from aiforge_core.llm import call_meter as _meter
    from aiforge_core.llm import rate_limiter as _rl

    def _on_request(request) -> None:
        # Order matches _post: throttle first, then count what is going out.
        try:
            _rl.acquire_global(max_wait_s=_wait_budget(), provider=provider)
        except Exception:  # noqa: BLE001 — a limiter fault must not kill a call
            pass
        try:
            tok = _meter.record(role, provider=provider, model=model)
            # httpx hands the SAME Request object to the response hook, and
            # `extensions` is a plain mutable dict on it — so this is how the
            # failure lands on the minute and turn of ITS OWN send rather than
            # of whenever the reply came back.
            request.extensions["aiforge_meter_token"] = tok
            pending.append(tok)
        except Exception:  # noqa: BLE001 — metering never breaks a call
            pass

    def _on_response(response) -> None:
        try:
            status = int(response.status_code)
        except Exception:  # noqa: BLE001
            return
        try:
            tok = response.request.extensions.get("aiforge_meter_token")
        except Exception:  # noqa: BLE001
            tok = None
        # Settled: this send has an answer, good or bad, so it is no longer the
        # transport's problem.
        try:
            pending.remove(tok)
        except ValueError:
            pass
        if status < 400:
            return
        body = _read_error_body(response)
        try:
            _meter.record_failure(tok, f"http_{status}")
        except Exception:  # noqa: BLE001
            pass
        _note_rate_limit(response, status, body, provider, _rl)

    return {"request": [_on_request], "response": [_on_response]}


def _settle_pending(pending: list, reason: str) -> None:
    """Mark every send that never got a response as failed.

    A connection reset or read timeout raises out of ``create()`` without ever
    reaching the response hook, so the request the operator most needs to see
    would otherwise sit in the meter as a success — the exact reading that
    hides a retry storm.
    """
    if not pending:
        return
    try:
        from aiforge_core.llm import call_meter as _meter
        for tok in list(pending):
            _meter.record_failure(tok, reason)
    except Exception:  # noqa: BLE001 — metering never breaks a call
        pass
    finally:
        pending.clear()


def _build_metered_client(base_url: str, timeout_s, role, model, provider,
                          pending: list):
    """An httpx client whose event hooks meter + throttle this path, or None to
    fall back to the SDK default.

    The OpenAI SDK builds its own httpx client that, by default, IGNORES
    AIForge's TLS policy — so a self-hosted HTTPS/self-signed model endpoint
    (AIFORGE_LLM_SSL_VERIFY=false / a CA bundle) fails with a bare "Connection
    error" while the litellm fallback connects. And it files the traffic under
    "OpenAI/Python", so an operator counting a user's calls silently misses
    every structured extraction. This client fixes both: litellm's verify
    policy, and our User-Agent + metering hooks.
    """
    try:
        import httpx
        from aiforge_core.net.ssl import httpx_verify
        from aiforge_core.llm.user_agent import user_agent
        return httpx.Client(verify=httpx_verify(base_url),
                            timeout=timeout_s or 120,
                            headers={"User-Agent": user_agent()},
                            event_hooks=_event_hooks(role, model, provider,
                                                     pending))
    except Exception:  # noqa: BLE001 — fall back to the SDK default client
        return None


def _charge_one_unmetered(role, model, provider: str) -> None:
    """No hooks means no ceiling on the sends about to happen. Charge ONE up
    front: undercounting a retry is a smaller error than exempting this whole
    path, which is the bug this file was rewritten to fix."""
    try:
        from aiforge_core.llm import rate_limiter as _rl
        _rl.acquire_global(max_wait_s=_wait_budget(), provider=provider)
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.llm import call_meter as _meter
        _meter.record(role, provider=provider, model=model)
    except Exception:  # noqa: BLE001
        pass


def _structured_max_tokens(max_tokens: int | None) -> int:
    """The max_tokens to request, floored. A structured (JSON) reply truncated
    by a too-small max_tokens raises IncompleteOutputException and forces the
    fallback loop (noisy + a wasted call), so short extractions get a sensible
    FLOOR — tunable via AIFORGE_STRUCTURED_MAX_TOKENS."""
    import os
    try:
        floor = max(256, int(os.environ.get("AIFORGE_STRUCTURED_MAX_TOKENS", "4096")))
    except (TypeError, ValueError):
        floor = 4096
    return max(int(max_tokens), floor) if max_tokens else floor


def structured(*, base_url: str, api_key: str, model: str,
               messages: list[dict], response_model: type[BaseModel],
               max_retries: int = 2, max_tokens: int | None = None,
               timeout_s: int | None = None,
               temperature: float | None = None,
               role: str | None = None,
               provider: str = "openai_compatible") -> BaseModel:
    """One validated completion. Raises ImportError when the lib is missing,
    or whatever instructor raises when retries exhaust."""
    import instructor
    from openai import OpenAI

    # The OpenAI SDK builds its own httpx client that, by default, IGNORES
    # AIForge's TLS policy — so a self-hosted HTTPS/self-signed model endpoint
    # (AIFORGE_LLM_SSL_VERIFY=false / a CA bundle) fails with a bare "Connection
    # error" while the litellm fallback connects. Hand OpenAI an httpx client
    # using the same verify policy litellm uses.
    _pending: list = []
    _http = _build_metered_client(base_url, timeout_s, role, model, provider,
                                  _pending)
    _oai_kwargs = {"base_url": base_url, "api_key": api_key or "not-needed",
                   "timeout": timeout_s or 120,
                   # The SDK retries on its OWN (default 2) INSIDE the call
                   # instructor is already reasking inside, and structured
                   # failures then fall through to `_fallback_complete`'s loop
                   # as well: three retry layers multiplying to up to nine
                   # sends for one extraction, each taking a ceiling slot. Our
                   # layers own the retrying; the SDK does not add a third.
                   "max_retries": _int_env("AIFORGE_STRUCTURED_SDK_RETRIES", 0)}
    if _http is not None:
        _oai_kwargs["http_client"] = _http
    else:
        _charge_one_unmetered(role, model, provider)
    cli = instructor.from_openai(OpenAI(**_oai_kwargs),
                                 mode=instructor.Mode.MD_JSON)
    kwargs: dict = {"max_tokens": _structured_max_tokens(max_tokens)}
    if temperature is not None:
        kwargs["temperature"] = temperature
    try:
        out = cli.chat.completions.create(
            model=model, messages=list(messages),
            response_model=response_model, max_retries=max_retries, **kwargs)
    except Exception as exc:  # noqa: BLE001 — count it, then re-raise unchanged
        _settle_pending(_pending, "transport_" + type(exc).__name__[:24])
        raise
    finally:
        # SETTLE, never discard. "A send whose response hook never ran and
        # whose call did not raise cannot happen" is false: the OpenAI SDK
        # retries APIConnectionError / APITimeoutError internally and returns
        # normally from the attempt that finally worked. Those sends never
        # reach the response hook, and clearing them here left the meter
        # reporting "3 sends, 0 failed" for a call that failed twice — the
        # precise reading _settle_pending exists to prevent, in the precise
        # situation (a retry storm) it was written for.
        _settle_pending(_pending, "no_response")
        try:
            # Closing is safe here because `out` is a fully-parsed model. It
            # would NOT be for a streaming/partial response_model
            # (Iterable[...] / Partial[...]), where instructor returns a lazy
            # generator — add a branch here before allowing those.
            if _http is not None:
                _http.close()
        except Exception:  # noqa: BLE001
            pass
    return out


__all__ = ["available", "structured"]
