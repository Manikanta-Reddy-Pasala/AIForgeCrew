"""Testing a provider connection before it is saved: the stored credentials a
role already has, and the plain and native tool-calling probes."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aiforge_core.config import agent_config as _acfg

router = APIRouter()


class _ProviderTestBody(BaseModel):
    base_url: str | None = Field(
        None, description="OpenAI-compatible base URL to probe; falls back "
                          "to the saved base_url for `role` when omitted")
    api_key: str | None = Field(
        None, description="Bearer key; falls back to the saved token for "
                          "`role` when omitted (UI never echoes the secret)")
    insecure_tls: bool = Field(
        False, description="Skip TLS verification for this probe only")
    role: str | None = Field(
        None, description="Archetype whose saved creds fill blank fields, "
                          "so Test works after Save without re-typing the token")


def _saved_role_credentials(role: str, base_url: str, api_key: str | None,
                            insecure: bool) -> tuple[str, str | None, bool]:
    """Fill blanks from the role's SAVED config (env + stored row).

    The UI never echoes the stored token back into the field, so without this
    fallback a Test issued right after Save would send no token and 401.
    """
    if not role or role not in _acfg.archetypes():
        return base_url, api_key, insecure
    try:
        rl = _acfg.resolve_litellm(role)
    except Exception:  # noqa: BLE001
        return base_url, api_key, insecure
    if not base_url:
        base_url = rl.get("api_base") or ""
    if not api_key:
        k = rl.get("api_key")
        api_key = None if (not k or k == "not-needed") else k
    return base_url, api_key, insecure or bool(rl.get("insecure_tls"))


@router.post("/api/providers/test")
def providers_test(body: _ProviderTestBody) -> dict:
    """Test-connection for the home page. Probes ``{base_url}/models`` and
    returns ``{ok, models[]}`` (or ``{ok:false, error}``).

    Blank ``base_url`` / ``api_key`` fall back to the saved config for
    ``role`` (resolved via env + stored row), so Test works right after
    Save.
    """
    from aiforge_core.llm.providers.openai_compatible import probe
    base_url, api_key, insecure = _saved_role_credentials(
        body.role, (body.base_url or "").strip(),
        (body.api_key or "").strip() or None, bool(body.insecure_tls))
    from aiforge_core.observability.logging import scrub
    logging.getLogger("aiforge.api").info(
        "POST /api/providers/test role=%s base_url=%s insecure_tls=%s token=%s",
        scrub(body.role), scrub(base_url), insecure, "yes" if api_key else "no")
    return probe(base_url, api_key, insecure=insecure)


def _key_for_base_url(base_url: str) -> str | None:
    """The saved API key of any role whose endpoint uses ``base_url`` — the UI
    masks the secret, so a per-model test recovers it from the role config that
    shares the URL. None when no saved role matches (a keyless local server)."""
    want = (base_url or "").rstrip("/")
    try:
        roles = _acfg.archetypes()
    except Exception:  # noqa: BLE001
        return None
    for role in roles:
        try:
            rl = _acfg.resolve_litellm(role)
        except Exception:  # noqa: BLE001
            continue
        if (rl.get("api_base") or "").rstrip("/") == want:
            k = rl.get("api_key")
            if k and k != "not-needed":
                return k
    return None


class _NativeTestBody(_ProviderTestBody):
    model: str | None = Field(
        None, description="Model id to test; falls back to the saved model "
                          "for `role` when omitted")


@router.post("/api/providers/test-native")
def providers_test_native(body: _NativeTestBody) -> dict:
    """Native tool-calling test for a model: POSTs a real one-tool request to
    ``{base_url}/chat/completions`` and reports whether the endpoint returns a
    ``tool_calls`` reply (native FC works), plain content (the model ignores
    tools), or an error. This is the diagnostic for "the model doesn't respond"
    when native tool-calling is on — chat sends tools, so an endpoint that only
    answers a plain {model,messages} request (like a bare curl) fails here.

    Blank ``base_url`` / ``api_key`` / ``model`` fall back to the saved config
    for ``role`` so the button works right after Save without re-typing.
    """
    from aiforge_core.llm.providers.openai_compatible import probe_native
    base_url, api_key, insecure = _saved_role_credentials(
        body.role, (body.base_url or "").strip(),
        (body.api_key or "").strip() or None, bool(body.insecure_tls))
    model = (body.model or "").strip()
    if not model and body.role and body.role in _acfg.archetypes():
        try:
            model = (_acfg.resolve_litellm(body.role).get("model") or "").strip()
        except Exception:  # noqa: BLE001
            model = ""
    # No explicit token and no role to borrow it from (a per-MODEL test button):
    # the UI never echoes the secret, so recover it from whichever saved role
    # points at this same base_url. Keys live on role rows, not model rows.
    if not api_key and base_url:
        api_key = _key_for_base_url(base_url)
    from aiforge_core.observability.logging import scrub
    logging.getLogger("aiforge.api").info(
        "POST /api/providers/test-native role=%s base_url=%s model=%s tls=%s",
        scrub(body.role), scrub(base_url), scrub(model), insecure)
    return probe_native(base_url, model, api_key, insecure=insecure)


@router.get("/api/agents/v2/providers")
def agents_v2_providers() -> list[dict]:
    """Catalog payload for the Settings UI: each provider with its
    available models inline. Includes dynamic discovery for local
    (LM Studio /v1/models) and ollama_cloud (5-min cached)."""
    out: list[dict[str, Any]] = []
    for prov in _acfg.list_providers():
        try:
            models = _acfg.list_models(prov["id"])
        except Exception:
            models = []
        out.append({**prov, "models": models})
    return out
