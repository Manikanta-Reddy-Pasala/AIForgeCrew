"""Vision-capability detection for a model — probe, cache, resolve, persist.

Split out of ``chat_media`` (which now only handles image storage / captioning
/ context injection) so capability detection is one focused concern. Three
signals, unified so they agree:

* the user's global ``vision_capable`` settings override;
* the registry's explicit per-model flag (``yes``/``no``) and, for ``auto``,
  its NAME heuristic (``model_registry.detect_capability``);
* a live probe that sends a real test image to the endpoint itself.

``vision_enabled(role)`` is the resolver the chat send-path uses;
``probe_vision_endpoint`` / ``classify_and_store_vision`` run at model-add time
to detect + persist capability for a model not yet wired to any role.
"""
from __future__ import annotations

import os

# 1x1 transparent PNG — inline fallback if the bundled probe image is missing.
# Some VLM servers reject a 1x1/degenerate image, so the real probe prefers the
# bundled fixture (assets/vision_probe.png, a small solid PNG).
_PROBE_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

_PROBE_ASSET = os.path.join(os.path.dirname(__file__), "assets", "vision_probe.png")


def _probe_image_b64() -> str:
    """Base64 of the bundled probe image — a real (non-degenerate) small PNG a
    VLM won't reject. Falls back to the inline 1x1 if the asset is missing."""
    try:
        import base64
        with open(_PROBE_ASSET, "rb") as fh:
            return base64.b64encode(fh.read()).decode()
    except Exception:  # noqa: BLE001
        return _PROBE_PNG


def _error_text(exc: Exception) -> str:
    """Full text of a probe error INCLUDING the HTTP response body — a urllib
    ``HTTPError`` is also a response object whose body carries the real message
    (e.g. LM Studio's 'does not support image inputs'), which ``str(exc)`` alone
    ('HTTP Error 400: Bad Request') drops. Without the body the definitive
    modality-rejection signal is lost and the model is wrongly left unknown."""
    t = str(exc)
    read = getattr(exc, "read", None)
    if callable(read):
        try:
            b = exc.read()
            t += " " + (b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b))
        except Exception:  # noqa: BLE001
            pass
    return t


# Phrases where the rejection binds to the MODALITY itself — a definitive
# "this model can't see images". Matched as substrings on the full error text
# incl. the HTTP body. Deliberately NOT bare tokens like "invalid" / "cannot":
# a real VLM that rejects the PROBE PAYLOAD ("invalid image_url", "cannot decode
# image", "unsupported image format") emits those, and marking it no-vision would
# be a sticky false-negative (it gets persisted to the registry).
_MODALITY_REJECTIONS = (
    "does not support image", "not support image", "doesn't support image",
    "does not support vision", "not support vision", "doesn't support vision",
    "does not support multimodal", "not multimodal", "no vision",
    "not a vision", "text-only", "text only model", "image input is not",
    "image inputs are not", "vision is not supported", "images are not supported",
)


def _classify_probe_error(exc: Exception) -> bool | None:
    """Read a failed vision probe. Returns ``False`` ONLY when the endpoint
    definitively rejected the IMAGE MODALITY (a modality-bound rejection PHRASE,
    e.g. 'does not support image inputs') — a transport blip, a bare 400, a 5xx/
    429, or a payload error ('invalid image_url', 'cannot decode image') is
    inconclusive (``None``) so a genuine VLM is never permanently marked
    non-vision. Classifies on the full error text incl. the HTTP body."""
    # A 5xx / 429 is transient (busy / loading / rate-limited), NEVER a
    # definitive modality rejection — a mid-inference crash body could otherwise
    # contain 'image' + 'cannot' and poison the cache as no-vision.
    code = getattr(exc, "code", 0)
    if code == 429 or (isinstance(code, int) and code >= 500):
        return None
    msg = _error_text(exc).lower()
    return False if any(p in msg for p in _MODALITY_REJECTIONS) else None


# model id -> probed vision capability. One live probe per model, then cached.
_VISION_CACHE: dict[str, bool] = {}


def _settings_override() -> bool:
    try:
        from aiforge_core.config import runtime_settings
        return int(runtime_settings.get("vision_capable")) > 0
    except Exception:  # noqa: BLE001
        return False


def _probe_vision(model: str, role: str) -> bool:
    """Ask the OpenAI-compatible endpoint itself whether it accepts image input
    — NO hardcoded model list. Delegates to :func:`probe_vision_endpoint` on the
    role's resolved endpoint so the probe hits the raw ``/chat/completions`` via
    the ``_post`` path — which preserves the HTTP error BODY (the definitive
    'does not support image inputs' signal that ``client.complete`` swallows into
    a generic ``llm.exhausted``). Caches only a DEFINITIVE result; a transient /
    ambiguous error stays inconclusive (uncached) so a genuine VLM isn't
    permanently marked non-vision."""
    if model in _VISION_CACHE:
        return _VISION_CACHE[model]
    try:
        from aiforge_core.llm.router import resolve
        ep = resolve(role)
        base_url = getattr(ep, "base_url", "") or ""
        api_key = getattr(ep, "api_key", "") or ""
    except Exception:  # noqa: BLE001
        return False
    verdict = probe_vision_endpoint(model, base_url, api_key)
    if verdict is True:
        _VISION_CACHE[model] = True
        return True
    if verdict is False:
        _VISION_CACHE[model] = False
        return False
    return False   # inconclusive — not cached, re-probe next time


def reset_vision_cache() -> None:
    _VISION_CACHE.clear()


def vision_enabled(role: str = "chat", *, probe: bool = False) -> bool:
    """True when the session's model can see images. The user's manual setting
    wins; otherwise it's probed from the OpenAI-compatible endpoint itself (no
    hardcoded allowlist). ``probe=False`` (default) only consults the settings
    override + a cached prior probe — fast, used on session-load. ``probe=True``
    runs the one-time live probe — used on the upload path where a brief delay
    is expected."""
    if _settings_override():
        return True
    try:
        from aiforge_core.llm.router import resolve
        ep = resolve(role)
        model = ep.model or ""
        base_url = getattr(ep, "base_url", "") or ""
    except Exception:  # noqa: BLE001
        return False
    if not model:
        return False
    # An explicit per-model vision flag from the registry wins over probing
    # (the user set it themselves for a model the probe can't resolve).
    try:
        from aiforge_core.config import model_registry
        flag = model_registry.vision_for(model, base_url)
        if flag == "yes":
            return True
        if flag == "no":
            return False
        # 'auto' (flag is None): trust the registry NAME heuristic when it
        # recognizes a known VLM family (qwen-vl, llava, pixtral, gemma-3, …) —
        # the same signal the Settings badge + ADK path use, so all three
        # detectors now agree. No probe needed on a hit.
        if model_registry.detect_capability(model, "vision"):
            return True
    except Exception:  # noqa: BLE001
        pass
    if probe:
        return _probe_vision(model, role)
    # UNKNOWN (auto, no heuristic hit, not cached): don't just assume no-vision
    # — kick a background probe so the capability is IDENTIFIED for the next
    # turn/upload. Return the current best guess (False) for THIS cheap call.
    warm_vision_async(role)
    return _VISION_CACHE.get(model, False)


def ensure_vision_known(role: str = "chat") -> bool | None:
    """Proactively DETERMINE + PERSIST a model's vision capability. Resolution:
    settings override → registry explicit flag → NAME heuristic (persisted) →
    cached probe → a live probe (persisted when definitive). Returns True/False
    when known, None when it couldn't be resolved (no model / inconclusive
    probe). Called at model-add, chat-model-set, session start and first turn so
    a model's vision support is known BEFORE an image is ever attached — and
    lazily identifies it mid-chat if still unknown."""
    if _settings_override():
        return True
    try:
        from aiforge_core.llm.router import resolve
        ep = resolve(role)
        model = ep.model or ""
        base_url = getattr(ep, "base_url", "") or ""
    except Exception:  # noqa: BLE001
        return None
    if not model:
        return None
    try:
        from aiforge_core.config import model_registry
        flag = model_registry.vision_for(model, base_url)
        if flag == "yes":
            return True
        if flag == "no":
            return False
        if model_registry.detect_capability(model, "vision"):
            # Name heuristic recognises a VLM family — persist it so it's durable.
            model_registry.set_vision_flag(model, base_url, "yes")
            _VISION_CACHE[model] = True
            return True
    except Exception:  # noqa: BLE001
        pass
    if model in _VISION_CACHE:
        return _VISION_CACHE[model]
    # Genuinely unknown → probe now (caches only a DEFINITIVE result).
    _probe_vision(model, role)
    if model in _VISION_CACHE:
        try:
            from aiforge_core.config import model_registry
            model_registry.set_vision_flag(
                model, base_url, "yes" if _VISION_CACHE[model] else "no")
        except Exception:  # noqa: BLE001
            pass
    return _VISION_CACHE.get(model)


def warm_vision_async(role: str = "chat") -> None:
    """Fire-and-forget :func:`ensure_vision_known` on a daemon thread — used at
    session start / model-set so identification never blocks the request. No-op
    if the capability is already known/cached for the role's model."""
    import threading
    threading.Thread(target=lambda: _safe_ensure(role),
                     name=f"vision-warm-{role}", daemon=True).start()


def _safe_ensure(role: str) -> None:
    import contextlib
    # warming must never surface an error
    with contextlib.suppress(Exception):
        ensure_vision_known(role)


def probe_vision_endpoint(model: str, base_url: str, api_key: str | None = None,
                          *, timeout_s: int | None = None) -> bool | None:
    """One-shot vision probe against an EXPLICIT endpoint — used at model-add
    time, before the model is wired to any role (so the role-based
    :func:`_probe_vision` can't reach it). Sends the bundled probe image to the
    server's ``/chat/completions`` reusing the client's auth + TLS handling.

    Returns ``True`` (accepts images) / ``False`` (definitively rejects) /
    ``None`` (inconclusive — transport error or ambiguous). Never raises."""
    if not model or not base_url:
        return None
    if timeout_s is None:
        try:
            timeout_s = int(os.environ.get("AIFORGE_VISION_PROBE_TIMEOUT_S", "8"))
        except (TypeError, ValueError):
            timeout_s = 8
    try:
        import json as _json

        from aiforge_core.llm.client import _http
        from aiforge_core.llm.types import Endpoint
    except Exception:  # noqa: BLE001
        return None
    ep = Endpoint(base_url=base_url, api_key=api_key or "", model=model,
                  provider="local", role="vision", extras={})
    payload = _json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Reply with the single word: ok"},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + _probe_image_b64()}},
        ]}],
        "max_tokens": 1,
    }).encode()
    try:
        _http._post(ep, payload, timeout_s)
        return True
    except Exception as exc:  # noqa: BLE001
        return _classify_probe_error(exc)


def classify_and_store_vision(registry_id: str, model: str, base_url: str,
                              api_key: str | None = None) -> bool | None:
    """Determine + PERSIST a model's vision capability at add-time, for EVERY
    model. A live endpoint probe wins; if it's inconclusive (endpoint not yet
    reachable), fall back to the NAME heuristic so a recognisable VLM is still
    marked ``yes``. Only writes a definite result; returns the verdict. Never
    raises (add-time background use)."""
    verdict = probe_vision_endpoint(model, base_url, api_key)
    if verdict is None:
        # Probe inconclusive → trust the name heuristic when it recognises a VLM.
        try:
            from aiforge_core.config import model_registry
            if model_registry.detect_capability(model, "vision"):
                verdict = True
        except Exception:  # noqa: BLE001
            verdict = None
        if verdict is None:
            return None
    try:
        from aiforge_core.config import model_registry
        model_registry.update_model(
            registry_id, vision=("yes" if verdict else "no"))
    except Exception:  # noqa: BLE001
        pass
    reset_vision_cache()
    return verdict
