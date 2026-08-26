"""User-managed model registry — the simplified Settings flow.

The user adds one or two models (each = an OpenAI-compatible endpoint: a model
id + base URL + optional API key + TLS + vision flag) ONCE, here. Every agent
then just *picks* a model by name — no per-agent URLs/keys. Applying a model to
a role writes that model's connection details into the role's agent_config via
``agent_config.set_role``.

Stored as JSON at ``$AIFORGE_CONFIG_DIR/model_registry.json``. API keys are kept
server-side and never returned (only ``api_key_set``).
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

_LOCK = threading.Lock()
_VISION = ("auto", "yes", "no")
_THINKING = ("auto", "yes", "no")

# Name heuristics for auto-detecting a reasoning/"thinking" model (emits a
# <think> channel). Used when thinking=='auto'. Substring match, lowercased.
_THINKING_MARKERS = (
    "qwythos", "ornith", "thinking", "reasoner", "reasoning", "-r1", "r1-",
    "deepseek-r1", "qwq", "o1", "o3", "o4-mini", "marco-o1", "sky-t1", "-think",
)
# NOTE: markers are substring-matched, so a bare "vl" wrongly flagged "vllm" /
# "nvl" etc. — use BOUNDARY forms (-vl / vl- / -vl-) so a served-by-vllm text
# model isn't mistaken for a vision-language model.
# Substring markers for a VLM NAME. Deliberately conservative — a name-heuristic
# hit is PERSISTED as vision WITHOUT probing, so a false match durably mis-marks
# a text model. Dropped: 'gemma-3'/'gemma3' (the family mixes text-only 1b with
# vision 4b+ under one prefix — let the live probe decide), and '-v-' (matched
# version tags like 'foo-v-2'). Vision-only sizes stay explicit.
_VISION_MARKERS = (
    "-vl", "vl-", "-vl-", "vision", "llava", "bakllava", "moondream",
    "pixtral", "internvl", "minicpm-v", "qwen2-vl", "qwen2.5-vl",
    "gemma-3-4b", "gemma-3-12b", "gemma-3-27b",   # vision gemma sizes only
    "llama-3.2-11b", "llama-3.2-90b", "-omni", "cogvlm", "nvlm",
)


def detect_capability(model_id: str, kind: str) -> bool:
    """Heuristic capability detection from the model id when the flag is 'auto'.
    kind = 'thinking' | 'vision'. Substring match on markers."""
    m = (model_id or "").lower()
    markers = _THINKING_MARKERS if kind == "thinking" else _VISION_MARKERS
    return any(k in m for k in markers)


def _path() -> str:
    root = str(config_dir())
    return os.path.join(root, "model_registry.json")


def _load() -> list[dict]:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 — missing/corrupt → empty
        return []


def _save(rows: list[dict]) -> None:
    _atomic.write_text(_path(), json.dumps(rows, indent=2))


def _slug(label: str, model: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (label or model or "model").lower()).strip("-")
    return base or "model"


def _resolve(flag: str, model_id: str, kind: str) -> bool:
    """Flag ('auto'|'yes'|'no') → effective bool. 'auto' → name heuristic."""
    f = (flag or "auto").lower()
    if f == "yes":
        return True
    if f == "no":
        return False
    return detect_capability(model_id, kind)


def _public(row: dict) -> dict:
    """Registry row without the raw key, with resolved capabilities."""
    mid = row.get("model") or ""
    vision = row.get("vision") or "auto"
    thinking = row.get("thinking") or "auto"
    return {"id": row.get("id"), "label": row.get("label") or row.get("model"),
            "model": row.get("model"), "base_url": row.get("base_url") or "",
            "insecure_tls": bool(row.get("insecure_tls", True)),
            "vision": vision, "thinking": thinking,
            # resolved booleans so the UI shows a badge + auto-select can match
            "has_vision": _resolve(vision, mid, "vision"),
            "has_thinking": _resolve(thinking, mid, "thinking"),
            "context_window": int(row.get("context_window") or 0),
            "api_key_set": bool(row.get("api_key"))}


def list_models() -> list[dict]:
    return [_public(r) for r in _load()]


def _chain_candidate(r, want: str, want_host: str) -> bool:
    """Whether registry row ``r`` is a usable fallback for the failed
    ``(want, want_host)`` model.

    A hand-edited registry can hold anything, so a non-dict / model-less row is
    skipped. Exclusion is by CONNECTION, not model id: the same model on a
    second box is the textbook redundancy setup, so only the row that IS the
    failed endpoint is skipped (a row with no base_url resolves to the failed
    host, so it counts as the same endpoint). Non-generative rows (embed /
    reranker / gte / e5) are dropped so they don't burn a round trip."""
    if not isinstance(r, dict):
        return False
    mid = str(r.get("model") or "").strip()
    if not mid:
        return False
    row_host = str(r.get("base_url") or "").strip().rstrip("/").lower() or want_host
    if mid.lower() == want and row_host == want_host:
        return False
    return _is_generative(mid)


def chain_after(model: str, base_url: str = "") -> list[dict]:
    """The OTHER configured models, in registry order, as raw rows.

    "I added four models; when the one chat picked stops answering it should try
    the others" — the registry was only ever a selection list, so a dead model
    was the end of the road on a single-provider install (the provider fallback
    chain is CLOUD escalation, empty with no cloud key). Raw rows (api_key
    included) because the caller builds an Endpoint from them.
    """
    want = (model or "").strip().lower()
    want_host = (base_url or "").strip().rstrip("/").lower()
    return [dict(r) for r in _load() if _chain_candidate(r, want, want_host)]


def get_model(model_id: str) -> dict | None:
    for r in _load():
        if r.get("id") == model_id:
            return r
    return None


def add_model(*, label: str, model: str, base_url: str = "",
              api_key: str | None = None, insecure_tls: bool = True,
              vision: str = "auto", thinking: str = "auto",
              context_window: int = 0) -> dict:
    model = (model or "").strip()
    if not model:
        raise ValueError("model id is required")
    if vision not in _VISION:
        vision = "auto"
    if thinking not in _THINKING:
        thinking = "auto"
    with _LOCK:
        rows = _load()
        mid = _slug(label, model)
        existing = {r["id"] for r in rows}
        uid, n = mid, 2
        while uid in existing:
            uid = f"{mid}-{n}"
            n += 1
        row = {"id": uid, "label": (label or model).strip(), "model": model,
               "base_url": (base_url or "").strip(), "api_key": api_key or "",
               "insecure_tls": bool(insecure_tls), "vision": vision,
               "thinking": thinking,
               "context_window": max(0, int(context_window or 0))}
        rows.append(row)
        _save(rows)
        return _public(row)


def _apply_model_fields(r: dict, fields: dict) -> None:
    """Apply the updatable fields onto registry row ``r`` in place, each with its
    own validation/coercion. A key is overwritten only when a non-empty one is
    supplied."""
    for k in ("label", "model", "base_url"):
        if fields.get(k) is not None:
            r[k] = str(fields[k]).strip()
    if fields.get("insecure_tls") is not None:
        r["insecure_tls"] = bool(fields["insecure_tls"])
    if fields.get("vision") in _VISION:
        r["vision"] = fields["vision"]
    if fields.get("thinking") in _THINKING:
        r["thinking"] = fields["thinking"]
    if fields.get("context_window") is not None:
        r["context_window"] = max(0, int(fields["context_window"] or 0))
    if fields.get("api_key"):
        r["api_key"] = fields["api_key"]


def update_model(model_id: str, **fields: Any) -> dict | None:
    with _LOCK:
        rows = _load()
        for r in rows:
            if r.get("id") != model_id:
                continue
            _apply_model_fields(r, fields)
            _save(rows)
            return _public(r)
    return None


def connection_for(model: str, base_url: str = "") -> dict | None:
    """The CONNECTION the user registered for ``model`` — its own endpoint, not
    somebody else's.

    Each registry row carries its own ``base_url``/key/TLS, but the paths that
    only pass a MODEL ID around (picking a chat model, a role row saved without
    a URL) used to fall back to whatever endpoint was already configured — so
    selecting a model added from a SECOND server silently kept the FIRST
    server's URL and every call went to a host that had never heard of it.

    Returns ``{"base_url", "api_key", "insecure_tls"}``, or None when:
      * the registry has no row for this model (the caller's own fallback is
        then correct — it may be an env-pinned model that was never added), or
      * several rows share the model id and ``base_url`` doesn't say which
        (the same id served from two endpoints is exactly the case that must
        not be guessed).
    A row registered WITHOUT a base_url yields None too: it has no endpoint of
    its own to prefer.
    """
    model = (model or "").strip()
    if not model:
        return None
    want = (base_url or "").strip()
    with _LOCK:
        rows = [r for r in _load()
                if (r.get("model") or "") == model
                and (not want or (r.get("base_url") or "") == want)]
    if len(rows) != 1:
        return None
    row = rows[0]
    if not (row.get("base_url") or "").strip():
        return None
    return {"base_url": row["base_url"].strip(),
            "api_key": row.get("api_key") or None,
            "insecure_tls": bool(row.get("insecure_tls"))}


def set_vision_flag(model: str, base_url: str, flag: str) -> bool:
    """Persist a resolved vision flag (``yes``/``no``) onto the row matched by
    model id (+ base_url when given). Used by the auto-detect path to make a
    probed/heuristic result durable so it survives a restart and shows the right
    badge. No-op (returns False) when ``flag`` is invalid or no row matches (an
    env-override model that isn't a registry row keeps only the in-memory cache)."""
    if flag not in ("yes", "no"):
        return False
    model = (model or "").strip()
    if not model:
        return False
    with _LOCK:
        rows = _load()
        for r in rows:
            if r.get("model") == model and (not base_url or r.get("base_url") == base_url):
                if r.get("vision") == flag:
                    return True
                r["vision"] = flag
                _save(rows)
                return True
    return False


def remove_model(model_id: str) -> bool:
    with _LOCK:
        rows = _load()
        new = [r for r in rows if r.get("id") != model_id]
        if len(new) == len(rows):
            return False
        _save(new)
        return True


def context_for(model: str, base_url: str = "") -> int:
    """Per-model context window (tokens) for a model matched by id+url, or 0
    when unset (caller falls back to the global setting)."""
    model = (model or "").strip()
    for r in _load():
        if r.get("model") == model and (not base_url or r.get("base_url") == base_url):
            return int(r.get("context_window") or 0)
    return 0


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
        return max(0, context_for(ep.model or "", base_url)), base_url, api_key
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


def effective_context_window(role: str | None = None) -> int:
    """The single source of truth for the input context window (tokens).

    Resolution order (first that yields a value wins) — an EXPLICIT operator
    choice ALWAYS beats auto-detection, which beats the static default:
      1a. per-model registry window for this role's model, else
      1b. the global ``runtime_settings`` explicit value, else
      2.  auto-detected from the live endpoint's ``/v1/models`` (capped 256K),
          gated by ``AIFORGE_AUTODETECT_CTX``, else
      3.  the static default (262144 = 256K).
    """
    per, base_url, api_key = _explicit_role_window(role)
    if per > 0:
        return per
    exp = _explicit_global_window()
    if exp is not None:
        return exp
    det = _autodetected_window(base_url, api_key)
    if det is not None:
        return det
    return _CTX_STATIC_DEFAULT


def context_window_for_role(role: str) -> int:
    """The effective input context window for ``role`` — the role's model's
    per-model value if set, else auto-detected, else the global setting. Thin
    wrapper over :func:`effective_context_window` (kept for back-compat)."""
    return effective_context_window(role)


def vision_for(model: str, base_url: str = "") -> str | None:
    """Explicit vision flag ('yes'/'no') for a model matched by id+url, or None
    when unset/auto — so callers can fall back to probing."""
    model = (model or "").strip()
    for r in _load():
        if r.get("model") == model and (not base_url or r.get("base_url") == base_url):
            v = r.get("vision") or "auto"
            return v if v in ("yes", "no") else None
    return None


def sync_from_config() -> dict:
    """Seed the registry from the agents' CURRENT per-role config — so a fresh
    registry isn't empty when models are already wired (e.g. via the legacy flow
    or env). Adds each distinct (model, base_url) that isn't registered yet.
    Returns ``{added: [ids], count}``."""
    try:
        from aiforge_core.config import agent_config
        cfg = agent_config.load_all()
    except Exception:  # noqa: BLE001
        return {"added": [], "count": 0}
    have = {(r.get("model"), r.get("base_url") or "") for r in _load()}
    added: list[str] = []
    for _role, c in cfg.items():
        model = (c.get("model") or "").strip()
        if not model or model.startswith("local-model-unconfigured"):
            continue
        key = (model, (c.get("base_url") or "").strip())
        if key in have:
            continue
        have.add(key)
        row = add_model(label=model.split("/")[-1], model=model,
                        base_url=c.get("base_url") or "", api_key=c.get("api_key"),
                        insecure_tls=bool(c.get("insecure_tls", True)))
        added.append(row["id"])
    return {"added": added, "count": len(added)}


def apply_to_roles(model_id: str, roles: list[str]) -> dict:
    """Point each role at this registry model (writes its connection details into
    agent_config). Returns ``{applied: [...], errors: {...}}``."""
    row = get_model(model_id)
    if row is None:
        raise ValueError(f"unknown model: {model_id}")
    from aiforge_core.config import agent_config
    applied, errors = [], {}
    for role in roles:
        try:
            agent_config.set_role(
                role, "openai_compatible", row["model"],
                base_url=row.get("base_url") or None,
                api_key=row.get("api_key") or None,
                insecure_tls=bool(row.get("insecure_tls")))
            applied.append(role)
        except Exception as exc:  # noqa: BLE001
            errors[role] = str(exc)
    return {"applied": applied, "errors": errors}


# Roles that benefit from a reasoning/"thinking" model (deep planning/judging).
_THINKING_ROLES = ("planner", "architect", "reviewer",
                   "validator", "critic", "reasoner", "judge", "orchestrator",
                   "gap_eval", "verify")
# QUICK, direct-output roles — a reasoning/"thinking" model is WRONG here: it
# spends its whole budget thinking and returns EMPTY on these short tasks
# (rephrase a query, distil a fact, classify, title). Force the fast
# NON-thinking model. (enhancer/learner were mis-classified as thinking —
# that's what made them return empty on a reasoning model.)
_FAST_ROLES = ("enhancer", "learner", "triage", "feedback", "refiner",
               "title", "summar", "classif", "ctx_", "live_verifier")
# Code-generation-heavy roles — a fast non-reasoning coder is better + cheaper.
_CODER_ROLES = ("doer", "developer", "coder", "implementer", "builder", "tester")


def is_fast_role(role: str) -> bool:
    """True when ``role`` is a QUICK, direct-output role (enhancer/learner/
    triage/feedback/refiner/title/summary/classify/…). These want a plain answer,
    NOT a reasoning trace — a reasoning model spends its budget thinking and
    returns empty. Callers use this to pre-empt the reasoning phase (send
    ``/no_think`` from the first attempt) so a fast role never wastes a round on
    an empty reasoning-model response."""
    rl = (role or "").strip().lower()
    return any(f in rl for f in _FAST_ROLES)


# Embedding / rerank models can't generate — never assign them to a chat role.
_NON_GENERATIVE_MARKERS = (
    "embed", "embedding", "rerank", "reranker", "bge-", "-bge", "nomic-embed",
    "gte-", "e5-", "instructor", "sentence-transformer",
)


def _is_generative(model_id: str) -> bool:
    m = (model_id or "").lower()
    return not any(k in m for k in _NON_GENERATIVE_MARKERS)


def _by_ctx(ms):
    """Models sorted by DESCENDING context window (largest first)."""
    return sorted(ms, key=lambda m: -(m.get("context_window") or 0))


def _assign_role(rl, vision, coder, think, models, default):
    """Best model id for one role name by capability: vision-needing -> vision,
    fast/direct -> non-thinking coder, thinking -> reasoning, coder -> coder,
    else the fast default."""
    if "vision" in rl and vision:
        return vision[0]["id"]
    if any(f in rl for f in _FAST_ROLES):
        return (coder or think or _by_ctx(models))[0]["id"]
    if any(t in rl for t in _THINKING_ROLES) and think:
        return think[0]["id"]
    if any(c in rl for c in _CODER_ROLES) and coder:
        return coder[0]["id"]
    return default


def suggest_assignments(roles: list) -> dict:
    """Map each role to the best available model BY CAPABILITY: thinking roles →
    a reasoning model, coder roles → a fast non-reasoning coder, vision-needing →
    a vision model. Larger context wins within a tier. {role: model_id}.
    Embedding/rerank models are excluded — they can't generate."""
    models = [m for m in list_models() if _is_generative(m.get("model") or m.get("id"))]
    if not models:
        return {}
    think = _by_ctx([m for m in models if m.get("has_thinking")])
    coder = _by_ctx([m for m in models if not m.get("has_thinking")])
    vision = _by_ctx([m for m in models if m.get("has_vision")])
    # DEFAULT for unclassified roles (e.g. chat) = the FAST non-thinking model,
    # not the largest-context one — a reasoning model as the blanket default is
    # what silently made simple chat answers come back empty. Only fall to a
    # thinking model when no fast one is configured.
    default = (coder or think or _by_ctx(models))[0]["id"]
    out: dict = {}
    for role in roles:
        out[role] = _assign_role((role or "").lower(), vision, coder, think,
                                 models, default)
    return out


def auto_assign(roles: list) -> dict:
    """Compute + APPLY capability-based assignments for ``roles``. Groups roles by
    chosen model and writes each into agent_config. Returns the plan + results."""
    plan = suggest_assignments(roles)
    by_model: dict = {}
    for role, mid in plan.items():
        by_model.setdefault(mid, []).append(role)
    results = {mid: apply_to_roles(mid, rs) for mid, rs in by_model.items()}
    return {"assignments": plan, "results": results}


__all__ = ["list_models", "get_model", "chain_after", "add_model",
           "connection_for",
           "update_model", "remove_model", "vision_for", "apply_to_roles",
           "detect_capability", "suggest_assignments", "auto_assign"]
