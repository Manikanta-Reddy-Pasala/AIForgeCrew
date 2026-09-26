"""Asking a model server how many requests it serves at once.

Each server says it differently, and most say nothing:

* LM Studio (``/api/v1/models``): every loaded instance carries its
  ``config.parallel``. A model that is not loaded, or an older LM Studio
  whose ``/api/v0/models`` has no such field, reads as one slot.
* llama.cpp server (``/props``): ``total_slots``.
* TGI (``/info``): ``max_concurrent_requests``.
* vLLM / SGLang (``/v1/models`` ``owned_by``, or a ``max_model_len`` field):
  continuous batching, so several slots.
* A hosted API over https on a public host: several slots.

Anything else is one slot. One slot is the safe answer: it only means the
caller keeps doing things one at a time, which is what it did before.
"""
from __future__ import annotations

import ipaddress
import os
import urllib.parse

#: owned_by values of servers that batch requests continuously.
_BATCHING_OWNERS = ("vllm", "sglang", "tgi", "text-generation-inference",
                    "tensorrt-llm", "lmdeploy")
#: owned_by values of servers known to serve one request at a time by default.
_SERIAL_OWNERS = ("library", "ollama", "organization_owner", "mlx-lm", "mlx")


def multi_default() -> int:
    """The slot count assumed for a server that batches but does not publish a
    number (vLLM, SGLang, a hosted API). ``AIFORGE_LLM_PARALLEL_MULTI``."""
    try:
        return max(2, int(os.environ.get("AIFORGE_LLM_PARALLEL_MULTI", "4")))
    except (TypeError, ValueError):
        return 4


def _get(url: str, base_url: str, api_key: str) -> object:
    """GET ``url`` as JSON, or None. Same short timeout and TLS rules as the
    context-window probe."""
    from . import health
    return health._get_json(url, base_url, api_key)


def _root(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _bare(model: str) -> str:
    """``openai/qwen/x@4bit`` → ``qwen/x``: the forms a model id reaches here in."""
    m = (model or "").strip()
    if m.startswith("openai/"):
        m = m[len("openai/"):]
    return m.split("@", 1)[0].lower()


def _int(v) -> int:
    if isinstance(v, bool):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _instance_matches(entry: dict, inst: dict, want: str) -> bool:
    if not want:
        return True
    names = (inst.get("id"), entry.get("key"), entry.get("selected_variant"))
    return any(isinstance(n, str) and _bare(n) == want for n in names)


def lmstudio_slots(body: object, model: str) -> "int | None":
    """Slots of ``model`` from an LM Studio ``/api/v1/models`` body, or None
    when the body is not LM Studio's. A model with no loaded instance is 1."""
    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        return None
    want = _bare(model)
    best = 0
    for entry in body["models"]:
        if not isinstance(entry, dict):
            continue
        for inst in entry.get("loaded_instances") or []:
            if isinstance(inst, dict) and _instance_matches(entry, inst, want):
                cfg = inst.get("config") or {}
                best = max(best, _int(cfg.get("parallel")) or 1)
    return max(1, best)


def _owners(body: object) -> list[dict]:
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return [e for e in body["data"] if isinstance(e, dict)]
    return []


def openai_models_slots(body: object) -> "int | None":
    """Several slots when ``/v1/models`` names a batching server; 1 when it
    names a serial one; None when it says nothing either way."""
    entries = _owners(body)
    owners = {str(e.get("owned_by") or "").strip().lower() for e in entries}
    if owners & set(_BATCHING_OWNERS) or any("max_model_len" in e for e in entries):
        return multi_default()
    if owners & set(_SERIAL_OWNERS):
        return 1
    return None


def _hosted(base_url: str) -> bool:
    """https on a public host name: a hosted API, which serves many callers."""
    try:
        u = urllib.parse.urlparse(base_url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or host == "localhost" or "." not in host:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        pass
    return not host.endswith((".local", ".lan", ".internal", ".home"))


def probe(base_url: str, model: str, api_key: str = "") -> int:
    """Concurrent request slots the server at ``base_url`` gives ``model``.
    Never raises; every miss is 1."""
    base = (base_url or "").rstrip("/")
    if not base:
        return 1
    models = _get(f"{base}/models", base, api_key)
    if models is None:
        return 1                      # unreachable: nothing to overlap with
    root = _root(base)
    lm = lmstudio_slots(_get(f"{root}/api/v1/models", base, api_key), model)
    if lm is not None:
        return lm
    by_owner = openai_models_slots(models)
    if by_owner is not None:
        return by_owner
    for path, field in (("/props", "total_slots"),
                        ("/info", "max_concurrent_requests")):
        body = _get(root + path, base, api_key)
        if isinstance(body, dict) and _int(body.get(field)) > 0:
            return _int(body.get(field))
    return multi_default() if _hosted(base) else 1


__all__ = ["lmstudio_slots", "multi_default", "openai_models_slots", "probe"]
