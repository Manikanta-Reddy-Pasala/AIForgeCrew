"""When the configured model is not served: diagnosing it, trying a served
substitute, and the errors raised when every provider failed."""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import replace
from typing import cast

from ..types import Endpoint
from ._errors import (
    _ModelReloading,
)
from ._http import TIMEOUT_SHIPPED_ATTR as _TIMEOUT_SHIPPED_ATTR
from ._models import MODEL_MISSING_ATTR


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.llm.client as package
    return package


def _autofallback_enabled() -> bool:
    """Stand in for a missing model with one the endpoint serves.

    On by default: a box that serves SOMETHING can usually still do the work,
    and the alternative is every role failing until a human edits a config
    file. ``AIFORGE_LLM_MODEL_AUTOFALLBACK=0`` turns it off for an operator who
    would rather a wrong model be a hard failure — a fair position when the
    model choice is the experiment.
    """
    import os as _os
    return _os.environ.get("AIFORGE_LLM_MODEL_AUTOFALLBACK", "1") not in (
        "0", "false", "no")


def _looks_like_a_model_error(shipped: dict) -> bool:
    """Is it worth asking the endpoint which models it serves?

    Only when the box ANSWERED and its answer was about the model: a 4xx (LM
    Studio's "No models loaded", a 404 for an unknown id) or the reloading
    exception that wording raises. Two exclusions, both load-bearing:

    * A SHIPPED read timeout proves the model exists — it accepted the prompt
      and is still generating. Calling that a missing model would rename the
      one failure the whole no-re-POST rule is built around.
    * A refused connection, a DNS failure or an unreachable host says nothing
      about model configuration, and probing on every such failure means an
      outbound request from code paths (including tests) that never asked for
      one.
    """
    if shipped.get("timeout"):
        return False
    exc = shipped.get("exc")
    if exc is None:
        return False
    if isinstance(exc, _ModelReloading):
        return True
    return isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500


def _diagnose_missing(shipped: dict, ep: Endpoint):
    """The endpoint's served models when the failure LOOKS like a model error,
    else None. A diagnostic must never mask the error."""
    if not _looks_like_a_model_error(shipped):
        return None
    try:
        from ._models import model_is_missing as _mim
        return _mim(ep.base_url, ep.model, ep.api_key or "")
    except Exception:  # noqa: BLE001
        return None


def _log_model_missing(role: str, ep: Endpoint, missing: list) -> None:
    _pkg()._log.error(
        "llm.model_missing role=%s model=%s endpoint=%s available=%s — "
        "CONFIGURATION: that model is not served here. Fix the role's model or "
        "load it; a fallback answering in its place is a rescue, not a fix.",
        role, ep.model, ep.base_url, ", ".join(missing[:8]),
        extra={"aiforge": {"role": role, "model": ep.model,
                           "endpoint": ep.base_url, "available": missing}})


def _substitute_attempt(role: str, primary: Endpoint, missing: list,
                        messages: list[dict], temperature: float | None,
                        max_tokens: int | None, top_p: float | None,
                        extras: dict | None, timeout_s: int) -> str | None:
    """The box told us what it DOES serve — use it rather than failing a whole
    run over one stale line of config. ONE retry, against the closest id the
    endpoint actually has. A rescue, not a routing decision: logged loudly at
    WARNING every time, because a silent substitution means the operator never
    learns their config is wrong and quietly gets a different model."""
    if not _autofallback_enabled():
        return None
    try:
        from ._models import pick_substitute as _pick
        sub = _pick(primary.model, missing)
    except Exception:  # noqa: BLE001
        return None
    if not sub:
        return None
    _pkg()._log.warning(
        "llm.model_substituted role=%s configured=%s served=%s using=%s "
        "endpoint=%s — the configured model is not served here; fix the role "
        "config or load it",
        role, primary.model, ",".join(missing[:8]), sub, primary.base_url,
        extra={"aiforge": {"role": role, "configured": primary.model,
                           "using": sub, "available": missing,
                           "endpoint": primary.base_url}})
    # `replace()` on a dataclass gives back the same type, but its declared
    # signature says only "a dataclass instance" — and _try_post's first
    # parameter is the one thing here that must be an Endpoint. cast() says so
    # without rebuilding the record field by field (which would silently drop
    # any field added to Endpoint later).
    substitute_ep = cast(Endpoint, replace(primary, model=sub))
    out = _pkg()._try_post(substitute_ep, messages, shipped={},
                    temperature=temperature, max_tokens=max_tokens,
                    top_p=top_p, extras=extras, timeout_s=timeout_s,
                    role=role, source="model_substitute")
    return out[0] if out is not None else None


def _model_missing_error(role: str, primary: Endpoint, missing: list):
    have = ", ".join(missing[:8]) if missing else "none loaded"
    exhausted = RuntimeError(
        f"llm.model_missing role={role} model={primary.model} "
        f"endpoint={primary.base_url} — that model is NOT served here "
        f"(available: {have}). This is configuration, not a transport "
        f"failure: point the role at one of the models above, or load it "
        f"on the endpoint. Retrying cannot fix it.")
    setattr(exhausted, MODEL_MISSING_ATTR, True)
    # What the box serves: [] means nothing is loaded right now (an idle
    # unload, a restart), which model_outage reads as an outage to wait out.
    exhausted.served_models = list(missing or [])
    _pkg()._log.error("llm.model_missing role=%s model=%s endpoint=%s available=%s",
               role, primary.model, primary.base_url, have,
               extra={"aiforge": {"role": role, "model": primary.model,
                                  "endpoint": primary.base_url,
                                  "available": missing}})
    return exhausted


def _exhausted_error(role: str, primary: Endpoint, fb, cloud,
                     chain_tried: int, shipped: dict):
    exhausted = RuntimeError(
        f"llm.exhausted role={role} primary={primary.provider}"
        f"@{primary.base_url} model={primary.model} "
        f"fallback={fb.provider if fb else 'none'} "
        f"cloud={cloud.provider if cloud else 'none'} "
        f"— all providers returned transport error or empty content "
        f"(see the llm.transport_error line above for the underlying cause)"
        + (f"; also tried {chain_tried} configured model(s) from the registry"
           if chain_tried else ""))
    if shipped.get("timeout"):
        # The prompt DID reach the model — callers above must not re-issue it.
        setattr(exhausted, _TIMEOUT_SHIPPED_ATTR, True)
    # The last transport error, for callers deciding whether to wait for the
    # endpoint. An attribute, not __cause__: the breaker walks the cause chain
    # and has already counted this failure.
    exhausted.transport_error = shipped.get("exc")
    return exhausted
