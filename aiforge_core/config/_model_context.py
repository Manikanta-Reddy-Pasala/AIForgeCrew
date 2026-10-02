"""Context windows: explicit, autodetected and static, per role; plus the
thinking and vision flags."""
from __future__ import annotations

import os


def _pkg():
    """``model_registry``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``model_registry``; patch any other
    name on this module."""
    import aiforge_core.config.model_registry as package
    return package


# Ceiling for a detected window (256K) and the static fallback default (128K).
# The default is the ASSUMED window for escalation/auto-condense sizing when a
# model has no explicit per-model value AND no global override AND detection is
# off/failed — deliberately CONSERVATIVE (128K): assuming LESS than the model's
# physically-loaded window only makes the app condense/cap earlier, which can
# never cause the "sent more than the served window" 400 that assuming MORE
# would. A model that genuinely wants a bigger window sets it per-model in the
# registry (highest-priority resolution path).
_CTX_CEILING = 262144
_CTX_STATIC_DEFAULT = 131072   # 128K default window


def _autodetect_ctx_enabled() -> bool:
    """Gate for the /v1/models context probe. Default ON; disable with
    ``AIFORGE_AUTODETECT_CTX=0``."""
    return os.environ.get("AIFORGE_AUTODETECT_CTX", "1") not in ("0", "false", "")


def _explicit_role_window(role: "str | None") -> "tuple[int, str, str]":
    """(per_model_window, base_url, api_key) for ``role``. The window is 0 when
    no explicit per-model registry value is set; base_url/api_key are captured so
    a later ctx probe can reach and auth to the same endpoint."""
    if not role:
        return 0, "", ""
    try:
        from aiforge_core.llm.router import resolve
        ep = resolve(role)
        base_url = getattr(ep, "base_url", "") or ""
        api_key = getattr(ep, "api_key", "") or ""
        return max(0, _pkg().context_for(ep.model or "", base_url)), base_url, api_key
    except Exception:  # noqa: BLE001
        return 0, "", ""


def _explicit_global_window() -> "int | None":
    """The explicit global operator context_window (UI store or env), or None —
    NOT the built-in default, so auto-detection can slot in below it."""
    try:
        from aiforge_core.config import runtime_settings
        exp = runtime_settings.explicit("context_window")
        return int(exp) if exp is not None else None
    except Exception:  # noqa: BLE001
        return None


def _autodetected_window(base_url: str, api_key: str) -> "int | None":
    """The live endpoint's advertised window (capped 256K), or None when
    autodetect is off, there is no base_url, or the probe soft-fails."""
    if not (_autodetect_ctx_enabled() and base_url):
        return None
    try:
        from aiforge_core.llm import health
        det = health.probe_context_window(base_url, api_key=api_key)
        return min(int(det), _CTX_CEILING) if det else None
    except Exception:  # noqa: BLE001
        return None


def context_window_source(role: str | None = None) -> "tuple[int, str]":
    """``(window, source)`` — the same resolution as
    :func:`effective_context_window`, plus WHERE the number came from:
    ``model`` (per-model setting), ``setting`` (global context_window),
    ``server`` (the endpoint's /v1/models — for LM Studio that is the context
    length the model was LOADED with, not its maximum) or ``default``. The chat
    meter shows it, so "why 32k when the model does 256k" answers itself."""
    per, base_url, api_key = _explicit_role_window(role)
    if per > 0:
        return per, "model"
    exp = _explicit_global_window()
    if exp is not None:
        return exp, "setting"
    det = _autodetected_window(base_url, api_key)
    if det is not None:
        return det, "server"
    return _CTX_STATIC_DEFAULT, "default"


def effective_context_window(role: str | None = None) -> int:
    """The single source of truth for the input context window (tokens).

    Resolution order (first that yields a value wins) — an EXPLICIT operator
    choice ALWAYS beats auto-detection, which beats the static default:
      1a. per-model registry window for this role's model, else
      1b. the global ``runtime_settings`` explicit value, else
      2.  auto-detected from the live endpoint's ``/v1/models`` (capped 256K),
          gated by ``AIFORGE_AUTODETECT_CTX``, else
      3.  the static default (131072 = 128K).
    """
    return context_window_source(role)[0]


def context_window_for_role(role: str) -> int:
    """The effective input context window for ``role`` — the role's model's
    per-model value if set, else auto-detected, else the global setting. Thin
    wrapper over :func:`effective_context_window` (kept for back-compat)."""
    return _pkg().effective_context_window(role)


def thinking_for(model: str, base_url: str = "") -> str | None:
    """The model's explicit reasoning setting ('yes'/'no'), or None when unset/
    auto. Matched by id (a LiteLLM ``openai/`` prefix is ignored) and, when
    given, base_url."""
    model = (model or "").strip()
    bare = model.split("/", 1)[1] if model.startswith("openai/") else model
    for r in _pkg()._load():
        row_url = (r.get("base_url") or "").rstrip("/")
        if r.get("model") in (model, bare) and (
                not base_url or not row_url or row_url == base_url.rstrip("/")):
            v = r.get("thinking") or "auto"
            return v if v in ("yes", "no") else None
    return None


def vision_for(model: str, base_url: str = "") -> str | None:
    """Explicit vision flag ('yes'/'no') for a model matched by id+url, or None
    when unset/auto — so callers can fall back to probing."""
    model = (model or "").strip()
    for r in _pkg()._load():
        if r.get("model") == model and (not base_url or r.get("base_url") == base_url):
            v = r.get("vision") or "auto"
            return v if v in ("yes", "no") else None
    return None


def parallel_for(model: str, base_url: str = "") -> int:
    """The model's registered concurrent-request slots, or 0 when unset (the
    caller then uses the global setting or probes the server). Matched like
    :func:`thinking_for`."""
    model = (model or "").strip()
    bare = model.split("/", 1)[1] if model.startswith("openai/") else model
    for r in _pkg()._load():
        row_url = (r.get("base_url") or "").rstrip("/")
        if r.get("model") in (model, bare) and (
                not base_url or not row_url or row_url == base_url.rstrip("/")):
            try:
                return max(0, int(r.get("parallel") or 0))
            except (TypeError, ValueError):
                return 0
    return 0
