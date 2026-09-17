"""The configured model chain: when a model fails, the next registry model for
the role is tried, on the text and the native tool-calling paths."""
from __future__ import annotations

import json
from dataclasses import replace

from ..types import Endpoint
from ._errors import (
    _LLMCancelled,
)


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.llm.client as package
    return package


def _model_chain_enabled() -> bool:
    """Try the operator's other configured models when the chosen one fails.

    On by default — four models were configured precisely so that one of them
    answering is enough. ``AIFORGE_LLM_MODEL_CHAIN=0`` restores "the selected
    model or nothing", which is the right setting when a run must be
    attributable to one exact model.
    """
    import os as _os
    return _os.environ.get("AIFORGE_LLM_MODEL_CHAIN", "1") not in (
        "0", "false", "no")


def _has_non_text_content(messages: list[dict]) -> bool:
    """Does this request carry image / multimodal parts?

    A vision call reached its role BECAUSE that model can see. Falling through
    to the operator's other models re-uploads multi-MB base64 to text-only
    endpoints, and the dangerous outcome is not the waste: a server that
    silently drops an unrecognised image block answers with a plausible
    caption of an image it never saw.
    """
    for m in messages or []:
        if isinstance(m.get("content"), list):
            return True
    return False


def _chain_rows(primary: Endpoint):
    """The registry rows to try after ``primary``, or [] when the registry is
    absent/unusable (one malformed registry is not a reason to fail the call)."""
    try:
        from aiforge_core.config import model_registry
        return model_registry.chain_after(primary.model, primary.base_url)
    except Exception:  # noqa: BLE001 — the registry is optional
        return []


def _chain_endpoint(row: dict, primary: Endpoint) -> "Endpoint | None":
    """The endpoint for one registry row, or None when the row is unusable.

    A row that names its own base_url is a DIFFERENT CONNECTION and is taken
    whole — its own key, its own TLS. Inheriting the primary's would put one
    endpoint's credential on another host's wire (and, through ``extras``, strip
    TLS verification from a public endpoint because a LAN box was marked
    insecure). A row without a base_url is the same endpoint with a different
    model id — the common case for several models on one local server.
    """
    if not isinstance(row, dict):
        return None                      # one malformed row is not a reason to
    mid = str(row.get("model") or "").strip()   # abandon every other model
    if not mid:
        return None
    # str() first: a hand-edited registry can hold a number here, and `.strip()`
    # on it raised an AttributeError from inside the LLM client in place of the
    # informative llm.exhausted the caller expects.
    row_url = str(row.get("base_url") or "").strip()
    if row_url and row_url.rstrip("/") != (primary.base_url or "").rstrip("/"):
        # Another host: take the connection whole. An empty key stays EMPTY — a
        # keyed primary must not lend its credential — and the row's own
        # insecure_tls decides its TLS, not the primary's.
        ex = dict(primary.extras or {})
        ex.pop("insecure_tls", None)
        if row.get("insecure_tls"):
            ex["insecure_tls"] = True
        return replace(primary, model=mid, base_url=row_url,
                       api_key=str(row.get("api_key") or ""), extras=ex)
    return replace(primary, model=mid)


def _model_chain_blocked(messages: list[dict], shipped: dict | None) -> bool:
    """Whether the chain must not run at all for this call."""
    if not _model_chain_enabled():
        return True
    if shipped and shipped.get("timeout"):
        # The prompt REACHED a model and was abandoned on a read timeout. Every
        # layer in this stack refuses to re-issue that; the chain must not be
        # the one place that does it N more times.
        return True
    return _has_non_text_content(messages)


def _try_model_chain(role: str, primary: Endpoint, messages: list[dict], *,
                     temperature, max_tokens, top_p, extras,
                     timeout_s: int, shipped: dict | None = None,
                     tried: list | None = None) -> "str | None":
    """One attempt against each OTHER configured model, in registry order."""
    if _model_chain_blocked(messages, shipped):
        return None
    for row in _chain_rows(primary):
        ep = _chain_endpoint(row, primary)
        if ep is None:
            continue
        mid = ep.model
        _pkg()._log.warning(
            "llm.model_chain_try role=%s failed=%s trying=%s endpoint=%s — "
            "the selected model did not answer; falling through to the next "
            "configured model",
            role, primary.model, mid, ep.base_url,
            extra={"aiforge": {"role": role, "failed": primary.model,
                               "trying": mid, "endpoint": ep.base_url}},
        )
        if tried is not None:
            tried.append(mid)
        # ONE post per chain model. The empty-retry ladder is for the model
        # the operator CHOSE; multiplying it across the chain turned one
        # message into 16+ full generations (4 models x 4 posts), and the
        # chat loop then re-issues the whole call up to five more times.
        out = _pkg()._try_post(ep, messages, shipped=(shipped if shipped is not None else {}),
                        empty_retries=0,
                        temperature=temperature,
                        max_tokens=max_tokens, top_p=top_p, extras=extras,
                        timeout_s=timeout_s, role=role, source="model_chain")
        if out is not None:
            _pkg()._log.warning("llm.model_chain_used role=%s model=%s (selected %s "
                         "did not answer)", role, mid, primary.model)
            return out[0]
    return None


def _native_chain_post(role: str, ep: Endpoint, alt: Endpoint, mid: str,
                       body_obj: dict, timeout_s: int, *,
                       meter: list) -> "dict | None":
    """Post one chain model on the native path, returning its answer or None.
    Rewrites the model id inside the already-built body so the tool definitions
    and every other parameter travel unchanged. Re-raises cancellation."""
    body_obj["model"] = mid
    _pkg()._log.warning(
        "llm.model_chain_try role=%s failed=%s trying=%s endpoint=%s "
        "(native tool path)", role, ep.model, mid, alt.base_url)
    try:
        out = _pkg()._post_with_retry(alt, json.dumps(body_obj).encode(),
                               timeout_s, role=role,
                               source="model_chain_native", meter=meter)
    except _LLMCancelled:
        raise
    except Exception:  # noqa: BLE001 — try the next configured model
        return None
    _pkg()._log.warning("llm.model_chain_used role=%s model=%s (selected %s did "
                 "not answer)", role, mid, ep.model)
    return out


def _native_model_chain(role: str, ep: Endpoint, payload: bytes,
                        timeout_s: int, *, meter: list) -> "dict | None":
    """The model chain for the NATIVE tool-calling path.

    Same rule as the text path — one attempt per configured model, own key and
    own TLS for a row that names its own host — but it rewrites the model id
    inside the already-built JSON body rather than rebuilding it.
    """
    if not _model_chain_enabled():
        return None
    try:
        body_obj = json.loads(payload.decode())
    except Exception:  # noqa: BLE001
        return None
    for row in _chain_rows(ep):
        alt = _chain_endpoint(row, ep)
        if alt is None:
            continue
        out = _native_chain_post(role, ep, alt, alt.model, body_obj,
                                 timeout_s, meter=meter)
        if out is not None:
            return out
    return None
