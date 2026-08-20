"""Chat routes (/api/chat/*) — split out of api.py (APIRouter).

Thin LLM proxy + the full-filesystem coding agent, the chat model pickers
(chat slot / orchestrator), persistent chat sessions (create/list/message/
media/checkpoints/steer/…), and per-mode approval settings. The giant session
message handler and every chat helper (workspace mgmt, history shaping, step
digest, served-model discovery) moved here VERBATIM; handlers keep their inline
function-local imports and behaviour.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from aiforge_core.api.routes._sse import sse_response
from pydantic import BaseModel, Field

from aiforge_core.config import agent_config as _acfg
from aiforge_core.runtime.background import spawn as _spawn
from aiforge_core.tickets import store as tickets_mod

router = APIRouter()

_af_log = logging.getLogger("aiforge")

# Global cap on concurrent chat producer threads — a producer keeps running
# after the client disconnects (by design, for navigate-away survival), so
# without a cap N fired sessions = N background agent loops driving the model
# with nobody attached. Excess producers block at the start until a slot frees.
try:
    _PRODUCE_SEM = threading.BoundedSemaphore(
        max(1, int(os.environ.get("AIFORGE_MAX_CHAT_RUNS", "8"))))
except ValueError:
    _PRODUCE_SEM = threading.BoundedSemaphore(8)


class _ApprovalModeBody(BaseModel):
    enabled: bool = Field(..., description="Require human approval for this mode")


@router.get("/api/chat/approval-settings")
def approval_settings_get() -> dict:
    """Per-chat-mode approval toggles (Chat/Plan/Pipeline). True = that mode
    pauses for human Approve/Reject on ask-policy / review-gated tools."""
    from aiforge_core.config import approval_settings
    m = approval_settings.all_modes()
    return {"chat": m["simple"], "plan": m["plan"], "pipeline": m["team"]}


@router.put("/api/chat/approval-settings/{mode}")
def approval_settings_set(mode: str, body: _ApprovalModeBody) -> dict:
    """Enable/disable approvals for one mode. `mode` is chat | plan | pipeline."""
    from aiforge_core.config import approval_settings
    try:
        approval_settings.set_mode(mode, body.enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return approval_settings_get()


class _ChatAskBody(BaseModel):
    query: str = Field(..., description="The operator's free-text question")
    top_k: int = Field(12, description="Memory hits per role")
    role: str = Field("planner", description="Retrieval policy role")


@router.post("/api/chat/ask")
def chat_ask(body: _ChatAskBody) -> dict:
    """Thin LLM proxy. No memory orchestration, no MCP tools — those
    live in the ticket pipeline now. Use POST /api/tickets for the
    full-featured agent flow."""
    from aiforge_core.orchestrator import llm_client
    answer = llm_client.call_text(
        role="doer",
        system="You are AIForgeCrew's chat assistant. Be concise.",
        user=body.query.strip() or "Hello",
        temperature=0.2,
        max_tokens=2048,
    )
    return {
        "answer": answer or "(empty response)",
        "trace": [],
        "hits": [],
    }


@router.post("/api/chat/retain", status_code=201)
def chat_retain(body: dict | None = None) -> dict:
    """Retention path was tied to the GA agent's auto-suggest. Now a
    no-op stub — explicit memory writes go through the new agent
    pipeline's Learner stage. (``_ChatRetainBody`` was deleted; the typed
    annotation became an undefined forward-ref that made FastAPI 422 the
    endpoint instead of returning the no-op — accept a plain body now.)"""
    return {"id": None, "retained": False, "reason": "deprecated"}


class _ChatMessage(BaseModel):
    role: str = Field("user", description="'user' or 'assistant'")
    content: str = Field("", description="message text")


class _ChatAgentBody(BaseModel):
    messages: list[_ChatMessage] = Field(..., description="conversation so far")
    cwd: str | None = Field(None, description="working directory; default workspace")
    role: str = Field("doer", description="archetype whose provider config drives the LLM")
    builder: str | None = Field(
        None, description="task charter: job|skill|workflow|rule (interactive "
        "builder that ends by calling the matching finalize tool)")


def _default_cwd() -> str:
    return (
        os.environ.get("AIFORGE_WORKSPACE_DIR")
        or os.environ.get("AIFORGE_REPO_ROOT")
        or os.getcwd()
    )


@router.post("/api/chat/agent")
def chat_agent(body: _ChatAgentBody) -> StreamingResponse:
    """Conversational full-filesystem coding agent (SSE).

    Streams ReAct steps — thoughts, tool calls + results, and the final
    message — as ``data: {json}\\n\\n`` events. Drives the provider
    configured for ``role`` on the home page. NOT the ticket pipeline.
    """
    from aiforge_core.runtime.chat_agent import run_chat_agent
    cwd = body.cwd or _default_cwd()
    msgs = [{"role": m.role, "content": m.content} for m in body.messages]

    def _gen():
        try:
            for ev in run_chat_agent(msgs, cwd=cwd, role=body.role,
                                     builder=body.builder):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'text': str(exc)})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return sse_response(_gen(), label="chat-agent")


class _NewSessionBody(BaseModel):
    title: str | None = Field(None)
    cwd: str | None = Field(None)
    role: str = Field("chat", description="model slot driving chat (default: chat)")


def _served_model_ids(provider: str) -> set:
    """IDs the provider is currently serving (active/loaded). For local /
    ollama_cloud this hits /v1/models; empty set when undiscoverable."""
    try:
        return {m.get("id") for m in (_acfg.list_models(provider) or [])
                if m.get("id")}
    except Exception:
        return set()


def _served_model_ids_for_role(role: str) -> set:
    """Served model IDs for a specific role's endpoint.

    openai_compatible has no static catalog — discover by probing the
    role's configured base_url (with its api_key + TLS settings) /models,
    exactly like the home-page Test. Falls back to provider-level
    discovery for local / ollama_cloud.
    """
    try:
        rl = _acfg.resolve_litellm(role)
    except Exception:
        rl = {}
    provider = (_acfg.get(role) or {}).get("provider") or "local"
    if provider == "openai_compatible":
        try:
            from aiforge_core.llm.providers.openai_compatible import probe
            res = probe(rl.get("api_base") or "", rl.get("api_key"),
                        insecure=bool(rl.get("insecure_tls")))
            return set(res.get("models") or [])
        except Exception:
            return set()
    return _served_model_ids(provider)


def _model_env_override(role: str) -> dict | None:
    """If an ``AIFORGE_<ROLE>_MODEL`` env var is set, it ALWAYS wins over the
    picker's persisted value (agent_config.load_all ops escape hatch). Return
    the pinning var + value so the UI can WARN that a pick won't take effect —
    otherwise the picker silently saves a model that never runs."""
    var = f"AIFORGE_{role.upper()}_MODEL"
    val = os.environ.get(var)
    if val and val.strip():
        return {"var": var, "model": val.strip()}
    return None


@router.get("/api/chat/models")
def chat_models() -> dict:
    """Models the user can pick for the 'chat' slot.

    Lists every model the user CONFIGURED (the model registry — the portable,
    machine-agnostic "models I added" surface managed in Settings) UNIONed with
    whatever the provider is currently serving, and flags each ``active`` =
    currently loaded. This is deliberately NOT loaded-only: a local model host
    (LM Studio) exposes only *loaded* models over HTTP, so listing served-only
    hides every model the user added but hasn't loaded. Selection does not load
    anything — that stays out of the UI; it only sets which model chat uses.
    Embedding models are excluded (not chat-capable). Provider-generic; no
    host-specific discovery.
    """
    row = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = row.get("provider") or "local"
    served = _served_model_ids_for_role("chat")
    current = row.get("model")

    def _chat_capable(mid: str) -> bool:
        return bool(mid) and "embed" not in mid.lower()

    out: dict[str, dict] = {}
    try:
        from aiforge_core.config import model_registry
        for r in model_registry.list_models():
            mid = (r.get("model") or "").strip()
            if not _chat_capable(mid) or mid in out:
                continue
            out[mid] = {"id": mid,
                        "label": (r.get("label") or mid.split("/")[-1]),
                        "active": mid in served}
    except Exception:  # noqa: BLE001 — registry optional; fall back to served
        pass
    # Any currently-served model not in the registry still belongs in the list.
    for mid in served:
        if _chat_capable(mid) and mid not in out:
            out[mid] = {"id": mid, "label": mid.split("/")[-1], "active": True}

    models = sorted(out.values(), key=lambda m: (not m["active"], m["id"]))
    # An env pin (AIFORGE_CHAT_MODEL / AIFORGE_DEFAULT_MODEL) overrides the
    # picker — surface it so the UI can show "env-pinned, picking won't apply".
    env_ovr = _model_env_override("chat") or _model_env_override("_default")
    return {
        "provider": provider,
        "current": current,
        "current_active": (current in served) if served else True,
        "models": models,
        "env_override": env_ovr,
    }


class _ChatModelBody(BaseModel):
    model: str = Field(..., min_length=1)
    provider: str | None = Field(None)
    apply_all: bool = Field(True, description="also set the global _default so "
                            "TEAM mode (all agents) uses this model")


@router.put("/api/chat/model")
def chat_model_set(body: _ChatModelBody) -> dict:
    """Persist the chat slot's model + report whether it's active (served
    right now). Rejected only on bad input — an inactive model is saved
    but flagged so the UI can warn."""
    cur = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = body.provider or cur.get("provider") or "local"
    try:
        # Preserve the chat endpoint's base_url / token / TLS opt-out — only
        # the model id is changing here. api_key=None is preserved by
        # set_role; insecure_tls must be passed through explicitly.
        cfg = _acfg.set_role("chat", provider, body.model,
                             base_url=cur.get("base_url"),
                             insecure_tls=bool(cur.get("insecure_tls")))
        # Apply to ALL agents by default: the picked model also becomes the
        # global _default so TEAM mode (triage/planner/doer/…) uses it too —
        # otherwise electing a bigger model only changes single-agent chat.
        if body.apply_all:
            gd = _acfg.get("_default") if "_default" in _acfg.archetypes() else {}
            _acfg.set_role("_default", provider, body.model,
                           base_url=cur.get("base_url") or gd.get("base_url"),
                           insecure_tls=bool(cur.get("insecure_tls")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    served = _served_model_ids_for_role("chat")
    # WARN when an env pin will override this pick — the persisted value is
    # saved but an AIFORGE_<ROLE>_MODEL env var wins on read, so the running
    # model won't change. Without this the picker silently no-ops.
    env_ovr = _model_env_override("chat")
    if body.apply_all and not env_ovr:
        env_ovr = _model_env_override("_default")
    warning = None
    if env_ovr and env_ovr.get("model") != cfg.get("model"):
        warning = (f"saved, but {env_ovr['var']}={env_ovr['model']} is set and "
                   "overrides it — the running model won't change until that "
                   "env var is unset")
    # Model changed → re-identify its vision capability (background).
    try:
        from aiforge_core.runtime import vision_detect
        vision_detect.reset_vision_cache()
        vision_detect.warm_vision_async("chat")
    except Exception:  # noqa: BLE001
        pass
    return {"provider": cfg.get("provider"), "model": cfg.get("model"),
            "applied_to": "all agents" if body.apply_all else "chat only",
            "active": (cfg.get("model") in served) if served else True,
            "env_override": env_ovr, "warning": warning}


class _ModelReloadBody(BaseModel):
    model: str = Field(..., min_length=1)
    context_length: int = Field(..., ge=1024, le=2_097_152,
                                description="LM Studio --context-length; "
                                "clamped up to the 64K project floor")
    ttl: int = Field(0, ge=0, description="--ttl seconds; 0 = no idle unload")


@router.post("/api/chat/model/reload")
def chat_model_reload(body: _ModelReloadBody) -> dict:
    """(Re)load a model on the LM Studio host at a chosen context window.

    Powers the UI 'context window' control: SSHes to AIFORGE_LMS_HOST,
    unloads any running copy of the model, then ``lms load`` at the
    requested ctx. Blocking until the load returns. 503 when no LMS host
    is configured (e.g. a cloud-only deploy), 502 on SSH/load failure."""
    from aiforge_core.runtime import local_starter as _ls
    res = _ls.load_model_now(body.model, body.context_length, ttl=body.ttl)
    if not res.get("ok"):
        err = res.get("error", "reload failed")
        code = 503 if "AIFORGE_LMS_HOST" in err else 502
        raise HTTPException(code, err)
    # A freshly (re)loaded model is now reachable — identify its vision
    # capability in the background so a definitive probe can persist.
    try:
        from aiforge_core.runtime import vision_detect
        vision_detect.reset_vision_cache()
        vision_detect.warm_vision_async("chat")
    except Exception:  # noqa: BLE001
        pass
    return res


_ORCHESTRATOR_ROLES = ("enhancer", "architect", "planner")


@router.get("/api/chat/orchestrator-model")
def orchestrator_model_get() -> dict:
    # The orchestrator picks from the SAME model universe as the worker/chat
    # slot — that's the real multi-model endpoint. Do NOT probe the planner
    # role: its base_url may be a per-model proxy (e.g. /proxy/<model>) that
    # serves one model and returns no /v1/models list, which would empty the
    # dropdown and spam "probe FAILED". Always include the current model so
    # the dropdown never renders empty.
    row = _acfg.get("planner") if "planner" in _acfg.archetypes() else {}
    served = set(_served_model_ids_for_role("chat"))
    current = row.get("model")
    if current:
        served.add(current)
    return {"provider": row.get("provider"), "model": current,
            "roles": list(_ORCHESTRATOR_ROLES),
            "models": [{"id": m, "label": m.split("/")[-1]} for m in sorted(served)]}


@router.put("/api/chat/orchestrator-model")
def orchestrator_model_set(body: _ChatModelBody) -> dict:
    """Set the model for the orchestrator's 2 agents (enhancer + planner)."""
    cur = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = body.provider or cur.get("provider") or "local"
    try:
        for role in _ORCHESTRATOR_ROLES:
            # Point at the CHAT slot's endpoint — the working multi-model
            # server. Reusing the role's own base_url would preserve a stale
            # per-model proxy (/proxy/<model>) and the picked model would 404.
            _acfg.set_role(role, provider, body.model,
                           base_url=cur.get("base_url"),
                           insecure_tls=bool(cur.get("insecure_tls")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "model": body.model, "roles": list(_ORCHESTRATOR_ROLES)}


class _RenameBody(BaseModel):
    title: str = Field(..., min_length=1)


class _SessionMsgBody(BaseModel):
    content: str = Field(..., min_length=1)
    role: str | None = Field(None, description="override the session's model (archetype)")
    mode: str = Field("simple", description="'simple' (single agent) | 'plan' (read-only single agent) | 'team' (full ADK flow)")
    review_edits: bool = Field(False, description="Hold every file-mutating tool call for human Approve/Reject (with diff) before it lands, in simple/plan mode. Default OFF — file writes/patches auto-apply. Opt in per-request here, or globally with AIFORGE_CHAT_REVIEW_EDITS=1.")
    edit_from_message_id: int | None = Field(None, description="Edit-and-resend: truncate history at this user message (restoring the workspace to that turn's checkpoint) before running this new content")
    builder: str | None = Field(None, description="task builder charter: job|skill|workflow|rule — runs an interactive single-agent builder that ends by calling the matching finalize tool (bypasses the enhancer/team pipeline)")
    quick: bool = Field(False, description="Quick mode: one doer, a hard step cap (AIFORGE_CHAT_QUICK_STEPS, default 6) instead of an open-ended ReAct loop. For small asks — a rename, a one-line fix, a question — where the agent's own exploration costs more than the change.")


def _quick_step_cap(quick: bool) -> int | None:
    """Hard step cap for a quick turn, or None for the normal open loop.

    The chat agent normally runs until it decides it is done (a stuck-loop
    detector bounds it, not a step count) — right for real work, and the reason
    a one-line ask can still cost minutes of exploration. Quick mode trades that
    thoroughness for latency: the agent gets a handful of steps, and if it needs
    more the user can simply ask again without the toggle.
    """
    import os as _os

    if not quick:
        return None
    try:
        return max(1, int(_os.environ.get("AIFORGE_CHAT_QUICK_STEPS", "6")))
    except ValueError:
        return 6


def _chat_workspace_root() -> str:
    return os.environ.get(
        "AIFORGE_CHAT_WORKSPACE_ROOT",
        os.path.join(os.path.expanduser(
            os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")), "chat-workspaces"))


def _delete_chat_workspace(cwd: str | None) -> bool:
    """``rm -rf`` a session's ISOLATED workspace when it is the managed,
    auto-created one under :func:`_chat_workspace_root`. Returns True if a dir
    was removed. Refuses anything else — a user-pinned project cwd, the root
    itself, or a path outside the managed tree — so clearing a chat can NEVER
    nuke a real repo. Leftover workspaces were the source of the "previous
    ticket's files leak into a new chat" bug; deleting them on clear removes it
    at the root (the per-turn baseline commit is the belt; this is the braces)."""
    if not cwd or not str(cwd).strip():
        return False
    import shutil
    try:
        root = os.path.realpath(_chat_workspace_root())
        target = os.path.realpath(str(cwd))
    except Exception:  # noqa: BLE001
        return False
    # Must be STRICTLY inside the managed root, and a session-* dir — never the
    # root itself, never a pinned repo, never a traversal escape.
    if target == root or not target.startswith(root + os.sep):
        return False
    if not os.path.basename(target).startswith("session-"):
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


def _is_isolated_workspace(cwd: str | None) -> bool:
    """True when ``cwd`` is a session's auto-created isolated scratch workspace
    (``chat-workspaces/session-<id>``) — NOT a real project. Such a session must
    not mint a phantom ``projects/session-<id>/`` OKR scope; its knowledge is
    GLOBAL. Same containment check as :func:`_delete_chat_workspace`."""
    if not cwd or not str(cwd).strip():
        return False
    try:
        root = os.path.realpath(_chat_workspace_root())
        target = os.path.realpath(str(cwd))
    except Exception:  # noqa: BLE001
        return False
    return (target != root and target.startswith(root + os.sep)
            and os.path.basename(target).startswith("session-"))


@router.post("/api/chat/sessions", status_code=201)
def chat_session_create(body: _NewSessionBody) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.create_session(body.title or "New chat",
                                  body.cwd or _default_cwd(),
                                  role=body.role or "chat")
    # Isolation: when the caller didn't pin a cwd, give the session its
    # own workspace dir so it can build/clean/run without touching other
    # sessions or the host. Persisted under app_state on the compose deploy.
    if not body.cwd:
        ws = os.path.join(_chat_workspace_root(), f"session-{s['id']}")
        try:
            os.makedirs(ws, exist_ok=True)
            s = chat_store.set_session_cwd(s["id"], ws) or s
        except OSError:
            pass
    # Opening a NEW chat = moving away from the previous one — fold that prior
    # session into memory in the background so its knowledge is recalled here.
    try:
        from aiforge_core.runtime import chat_session_fold
        chat_session_fold.fold_previous_async(s["id"])
    except Exception:  # noqa: BLE001 — a fold must never break session create
        pass
    # Identify the chat model's vision capability NOW (background), so it's known
    # before the user attaches an image — not discovered only on first upload.
    try:
        from aiforge_core.runtime import vision_detect
        vision_detect.warm_vision_async(s.get("role") or "chat")
    except Exception:  # noqa: BLE001
        pass
    return s


@router.get("/api/chat/sessions")
def chat_session_list() -> list[dict]:
    from aiforge_core.runtime import chat_store
    return chat_store.list_sessions()


@router.post("/api/chat/sessions/reset")
def chat_sessions_reset() -> dict:
    """Delete ALL chat sessions + messages and reset the id sequence, AND rm -rf
    every managed session workspace so no stale files survive the clear."""
    from aiforge_core.runtime import chat_store
    # Snapshot each session's cwd before the rows go, so we delete exactly the
    # managed workspaces they owned (a pinned user repo is refused by the helper).
    cwds = [(s or {}).get("cwd") for s in (chat_store.list_sessions() or [])]
    deleted = chat_store.delete_all_sessions()
    # Wipe compaction offsets too — ids restart at 1 after a reset, so a leftover
    # marker would make the new session-1 skip folding (silent knowledge loss).
    try:
        from aiforge_core.runtime import chat_okr
        chat_okr.clear_all_markers()
    except Exception:  # noqa: BLE001
        pass
    removed = 0
    for _cwd in cwds:
        if _delete_chat_workspace(_cwd):
            removed += 1
    # Belt-and-braces: also sweep any orphaned session-* dirs left under the
    # managed root (e.g. from a session whose row was already gone).
    try:
        import shutil
        _root = _chat_workspace_root()
        for _name in os.listdir(_root):
            if _name.startswith("session-"):
                _p = os.path.join(_root, _name)
                if os.path.isdir(_p):
                    shutil.rmtree(_p, ignore_errors=True)
                    removed += 1
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "deleted": deleted, "workspaces_removed": removed}


@router.get("/api/chat/sessions/{session_id}")
def chat_session_get(session_id: int) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.get_session(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id} not found")
    return {"session": s, "messages": chat_store.get_messages(session_id)}


@router.post("/api/chat/sessions/{session_id}/compact")
def chat_session_compact(session_id: int) -> dict:
    """Session-end OKR compaction (explicit trigger): distil this session into
    scoped OKR briefs (global / project / topic) via chat_okr.compact_session."""
    from aiforge_core.runtime import chat_okr, chat_store
    from aiforge_core.runtime.chat_agent import _chat_repo_key
    sess = chat_store.get_session(session_id)
    if not sess:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = sess.get("cwd")
    repo = _chat_repo_key(cwd) if cwd else None
    return chat_okr.compact_session(session_id, repo=repo)


@router.get("/api/chat/sessions/{session_id}/trace")
def chat_session_trace(session_id: int) -> dict:
    """Reviewable per-turn action+response trace (from ~/.aiforge/chat_traces).
    Each turn = {ts, mode, prompt, actions[], response, n_tools}."""
    from aiforge_core.runtime import chat_trace
    turns = chat_trace.read_turns(session_id)
    return {"session_id": session_id, "count": len(turns), "turns": turns}


@router.get("/api/chat/sessions/{session_id}/llm-usage")
def chat_session_llm_usage(session_id: int) -> dict:
    """How many requests this chat has sent to the LLM.

    ``turn`` = since the current/most recent turn started, ``session`` = since
    the API started, ``per_minute`` = machine-wide rate over the last 60s (what
    a rate-limited provider — and the user's fan — actually feels). Counted at
    the wire, so retries and fallbacks count, and reset on API restart.
    """
    from aiforge_core.llm import call_meter
    return {"session_id": session_id, **call_meter.snapshot(session_id)}


@router.get("/api/chat/sessions/{session_id}/spec")
def chat_session_spec(session_id: int) -> dict:
    """The planner's SPEC.md (requirements + subtask breakdown) for this
    session's workspace — rendered as a markdown preview in the subtask dock."""
    from aiforge_core.runtime import chat_store
    sess = chat_store.get_session(session_id) or {}
    cwd = sess.get("cwd") or _default_cwd()
    path = os.path.join(cwd, "SPEC.md")
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                return {"exists": True, "path": path, "content": fh.read()[:200000]}
    except Exception as exc:  # noqa: BLE001
        return {"exists": False, "error": str(exc)}
    return {"exists": False, "content": ""}


@router.patch("/api/chat/sessions/{session_id}")
def chat_session_rename(session_id: int, body: _RenameBody) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.rename_session(session_id, body.title)
    if not s:
        raise HTTPException(404, f"session {session_id} not found")
    return s


@router.delete("/api/chat/sessions/{session_id}", status_code=204)
def chat_session_delete(session_id: int) -> None:
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        chat_runs,
        chat_store,
    )
    # Stop any in-flight run first so its background producer doesn't keep
    # running + persisting against a session that no longer exists.
    chat_cancel.cancel(session_id)
    chat_approve.cancel(session_id)
    chat_interject.clear(session_id)
    chat_runs.finish(session_id)
    # Grab the isolated-workspace path BEFORE deleting the row so we can rm -rf
    # it — a lingering workspace's files otherwise leak into a future chat.
    _sess = chat_store.get_session(session_id)
    # Fold this session's knowledge into memory BEFORE its rows go — otherwise
    # deleting a chat silently discards everything worked out in it. Blocking +
    # idempotent + never raises; skip via AIFORGE_SESSION_COMPACT_ON_SWITCH=0.
    from aiforge_core.runtime import chat_session_fold
    if chat_session_fold._enabled():
        chat_session_fold.fold_sync(session_id)
    if not chat_store.delete_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    # Drop the session's compaction-offset marker so the marker file doesn't
    # accumulate entries for deleted sessions.
    try:
        from aiforge_core.runtime import chat_okr
        chat_okr.forget_session(session_id)
    except Exception:  # noqa: BLE001
        pass
    _delete_chat_workspace((_sess or {}).get("cwd"))


@router.post("/api/chat/sessions/{session_id}/media", status_code=201)
async def chat_media_upload(session_id: int, file: UploadFile = File(...)) -> dict:
    """Attach a file (image OR document — pdf/xlsx/docx/text) to a chat session:
    save it to the session's media folder, derive a description (vision caption
    for an image, extracted text for a document), and store the row. The
    description is what makes it queryable later in the session."""
    from aiforge_core.runtime import chat_media, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    raw = await file.read()
    saved = chat_media.save_file(session_id, file.filename or "file", raw)
    if not saved.get("ok"):
        raise HTTPException(400, saved.get("error", "invalid file"))
    role = (chat_store.get_session(session_id) or {}).get("role") or "chat"
    try:
        # describe_upload runs a (slow) vision/text extraction — off the event
        # loop so one image upload doesn't block every other request.
        desc = await asyncio.to_thread(
            chat_media.describe_upload, saved["path"], saved["filename"],
            saved["mime"], role)
    except Exception:  # noqa: BLE001 — describe/extract is best-effort
        desc = ""
    row = chat_store.add_media(session_id, saved["filename"], saved["path"],
                               mime=saved["mime"], description=desc)
    row["kind"] = saved.get("kind")
    row["auto_described"] = bool(desc)
    return row


@router.get("/api/chat/sessions/{session_id}/media")
def chat_media_list(session_id: int) -> dict:
    from aiforge_core.runtime import chat_media, chat_store
    return {"media": chat_store.list_media(session_id),
            "vision": chat_media.vision_enabled(
                (chat_store.get_session(session_id) or {}).get("role") or "chat")}


class _MediaDescBody(BaseModel):
    description: str = Field("", description="user caption / edited description")


@router.patch("/api/chat/media/{media_id}")
def chat_media_describe(media_id: int, body: _MediaDescBody) -> dict:
    from aiforge_core.runtime import chat_store
    row = chat_store.set_media_description(media_id, body.description)
    if row is None:
        raise HTTPException(404, f"media {media_id} not found")
    return row


@router.delete("/api/chat/media/{media_id}", status_code=204)
def chat_media_delete(media_id: int) -> None:
    from aiforge_core.runtime import chat_store
    row = chat_store.delete_media(media_id)
    if row is None:
        raise HTTPException(404, f"media {media_id} not found")
    try:  # best-effort unlink the file
        if row.get("path") and os.path.isfile(row["path"]):
            os.remove(row["path"])
    except Exception:  # noqa: BLE001
        pass


@router.get("/api/chat/media/{media_id}/raw")
def chat_media_raw(media_id: int) -> FileResponse:
    from aiforge_core.runtime import chat_store
    row = chat_store.get_media(media_id)
    if row is None or not os.path.isfile(row.get("path") or ""):
        raise HTTPException(404, "media not found")
    return FileResponse(row["path"], media_type=row.get("mime") or "image/png")


def _step_digest(steps: list) -> str:
    """One compact line summarising what an assistant turn DID — tool calls +
    outcomes — so the next turn's history carries the agent's actions, not just
    its final prose. Fixes the 'forgets what it just did' amnesia: persisted
    `steps` were never fed back into context, so any work the model didn't
    transcribe into its final answer vanished."""
    if not isinstance(steps, list):
        return ""
    bits: list[str] = []
    for s in steps:
        if not isinstance(s, dict) or s.get("type") != "tool":
            continue
        name = s.get("name") or "tool"
        res = s.get("result") or {}
        # Tiny outcome marker: ok / err / a key field, kept short.
        mark = ""
        if isinstance(res, dict):
            if res.get("ok") is False or res.get("error"):
                mark = "✗"
            elif res.get("ok") is True:
                mark = "✓"
        arg = ""
        a = s.get("args") or {}
        if isinstance(a, dict):
            for k in ("path", "file", "cmd", "command", "query", "pattern"):
                if a.get(k):
                    arg = str(a[k])[:48]
                    break
        bits.append(f"{name}({arg}){mark}" if arg else f"{name}{mark}")
        if len(bits) >= 12:
            bits.append("…")
            break
    return ", ".join(bits)


def _chat_history_for_agent(rows: list) -> list[dict]:
    """Build the agent's conversation history from persisted messages.

    Unlike a naive role+content copy, this (1) folds each assistant turn's tool
    DIGEST into its content so the agent remembers its own prior actions, (2)
    keeps assistant turns that did work but produced no final text (don't drop
    them — that left a gap AND broke user/assistant alternation), and (3) merges
    consecutive same-role turns (some providers reject two in a row)."""
    out: list[dict] = []
    for m in rows:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if role == "assistant":
            digest = _step_digest(m.get("steps") or [])
            if digest:
                content = (content + f"\n[did: {digest}]").strip() if content \
                    else f"[did: {digest}]"
        if not content:
            continue   # truly empty (e.g. a user turn with no text) — skip
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content   # merge same-role
        else:
            out.append({"role": role, "content": content})
    return out


@router.post("/api/chat/sessions/{session_id}/message")
def chat_session_message(session_id: int, body: _SessionMsgBody) -> StreamingResponse:
    """Append a user message, run the full-FS coding agent over the whole
    session history (Claude-CLI-style: many tool steps, builds repos),
    stream every step as SSE, and persist the assistant reply + steps.
    Auto-titles a fresh session. The model is the session's role
    (model picker)."""
    from aiforge_core.runtime import chat_store
    from aiforge_core.runtime.chat_agent import run_chat_agent
    from aiforge_core.runtime.chat_pipeline import stream_chat_pipeline

    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")

    # Reject an overlapping run for the same session (two tabs, or a reattach
    # racing a send). Letting a 2nd producer start would replace this session's
    # cancel/approve token, so the 1st run's Stop becomes a no-op and BOTH
    # producers persist a turn (duplicate/garbled history). The client already
    # guards on `busy`; this is the server-side backstop. Use /attach to watch
    # the in-flight run instead.
    from aiforge_core.runtime import chat_runs
    if chat_runs.is_running(session_id):
        raise HTTPException(409, "a run is already in progress for this session "
                                 "— stop it or attach to it before sending again")

    role = body.role or session.get("role") or "chat"
    if body.role and body.role != session.get("role"):
        chat_store.set_session_role(session_id, body.role)

    # Edit-and-resend: when the client edits an earlier user turn, restore the
    # workspace to that turn's checkpoint (so the re-run starts from the same
    # state the original did) and truncate the conversation at that message
    # before appending the edited content. Best-effort restore — a missing
    # checkpoint just means history truncation without a workspace rollback.
    if body.edit_from_message_id:
        try:
            _cwd_er = session.get("cwd") or _default_cwd()
            _sha = chat_store.message_checkpoint(session_id, body.edit_from_message_id)
            # Only roll the workspace back for a session's OWN isolated scratch.
            # A SHARED context dir (work/<kind>/<key>) or a real repo is touched
            # by other sessions/the operator; `git restore --worktree` there would
            # clobber their uncommitted work, and a checkpoint SHA taken in the
            # session's old repo doesn't even exist in a rebound one. Truncate
            # history either way; skip the destructive worktree restore.
            from aiforge_core.runtime import work_context as _wc0
            _own_scratch = (_wc0.context_for_path(_cwd_er) is None
                            and os.path.basename(os.path.normpath(_cwd_er))
                            .startswith("session-"))
            if _sha and _own_scratch:
                from aiforge_core.runtime import checkpoints as _ckpt
                _ckpt.restore(_cwd_er, _sha)
            chat_store.delete_messages_from(session_id, body.edit_from_message_id)
        except Exception as _exc:  # noqa: BLE001 — edit-resend must fail open
            _af_log.warning("edit-resend failed (session=%s msg=%s): %s",
                            session_id, body.edit_from_message_id, _exc)

    # Custom slash commands (Claude Code / Cursor parity, LOCAL files only).
    # A leading "/<name> args" whose <name> matches a user-defined command file
    # (.aiforge/commands/<name>.md or .claude/commands/<name>.md) expands to that
    # markdown template with $ARGUMENTS/$1.. substituted. Do it HERE — before the
    # message is persisted, titled, folded into `history`, or read as `prompt` —
    # so ONE interception covers simple, plan AND team modes (all of them derive
    # their prompt from body.content / the persisted history downstream). A
    # non-command message, a "/" typo, or an unknown /name expands to None and is
    # left verbatim. The built-in /help (and /commands) needs no user file and is
    # answered inline without invoking the model. Fail-open: any error → raw text.
    _cmd_expanded: str | None = None
    _cmd_help_text: str | None = None
    try:
        from aiforge_core.runtime import commands as _commands
        _cmd_cwd = session.get("cwd") or _default_cwd()
        _cmd_exp = _commands.expand(body.content, _cmd_cwd)
        if _cmd_exp is not None:
            _cmd_name = body.content.strip()[1:].split(None, 1)[0]
            _known = _cmd_name in _commands.load(_cmd_cwd)
            if not _known and _commands.is_builtin(_cmd_name):
                _cmd_help_text = _cmd_exp          # /help — answered inline
            else:
                body.content = _cmd_exp            # replace with expanded template
                _cmd_expanded = _cmd_name
    except Exception as _cexc:  # noqa: BLE001 — expansion must never break a turn
        _af_log.debug("slash-command expand skipped: %s", _cexc)

    # Persist the run mode on the user turn so the UI can badge which mode each
    # turn/session ran in (was composer-only client state, never stored).
    _turn_mode = body.mode if body.mode in ("simple", "plan", "team") else "simple"
    import time as _time
    _turn_t0 = _time.time()   # wall-clock start → per-turn duration (all 3 modes)
    _user_msg_id = chat_store.add_message(session_id, "user", body.content,
                                          mode=_turn_mode)
    # Provisional title now (instant), upgraded to a model-generated one after
    # the turn (see _produce). _fresh marks a still-unnamed session.
    _fresh_title = (session.get("title") or "New chat") == "New chat"
    if _fresh_title:
        # Clean deterministic provisional (strips 'Build a…', trailing clauses,
        # Title-Cases) — reads well instantly; upgraded by the model title below
        # when that succeeds. Beats the raw truncated first message.
        try:
            from aiforge_core.runtime import chat_title as _ct
            _prov = _ct.provisional_title(body.content) or body.content.strip()[:60]
        except Exception:  # noqa: BLE001
            _prov = body.content.strip()[:60]
        chat_store.rename_session(session_id, _prov)

    # Fold each assistant turn's tool digest into history + keep did-work-but-
    # blank turns + merge same-role runs, so the agent remembers what it DID
    # (not just what it said) on follow-ups.
    history = _chat_history_for_agent(chat_store.get_messages(session_id))
    cwd = session.get("cwd") or _default_cwd()
    team = body.mode == "team"
    from aiforge_core.runtime import parallel_subtasks as _psub
    agent_mode = "plan" if body.mode == "plan" else "act"
    prompt = body.content.strip()

    # Context-keyed workspace: if this chat is about a durable context (a Jira
    # ticket key like PROJ-42, or a Confluence page) and the session is still on
    # an EPHEMERAL folder (the default/session-<id> scratch), switch its cwd to
    # the SHARED ~/.aiforge/work/<kind>/<key>/ folder — so that ticket's images,
    # pages and scratch persist across every session that touches it. A session
    # already pinned to a context or to a real repo the user chose is left as-is.
    try:
        from aiforge_core.runtime import work_context as _wc
        # Ephemeral == the session's OWN scratch dir (a session-<id> folder INSIDE
        # the managed chat-workspace root) — NOT the configured default repo and
        # NOT a real repo the user pinned. Only such scratch is safe to re-home;
        # hijacking a real repo would strand the work in an empty folder.
        _ws_root = os.path.realpath(_chat_workspace_root())
        _cwd_real = os.path.realpath(cwd)
        _ephemeral = (
            _wc.context_for_path(cwd) is None
            and _cwd_real.startswith(_ws_root + os.sep)
            and os.path.basename(_cwd_real).startswith("session-"))
        if _ephemeral:
            _ctx = _wc.detect_context(prompt)
            if _ctx:
                cwd = _wc.context_dir(*_ctx)
                chat_store.set_session_cwd(session_id, cwd)
                _af_log.info("chat session %s bound to %s workspace %s",
                             session_id, _ctx[0], cwd)
    except Exception as _exc:  # noqa: BLE001 — never block a turn on this
        _af_log.debug("work-context bind skipped: %s", _exc)

    # Per-turn auto-route: once a team session has produced output, a small
    # follow-up ("rename that", "add a test") shouldn't re-run the whole heavy
    # pipeline (worktree + planner + verifier + slow Doer loop = minutes). A
    # cheap classify downgrades simple follow-ups to the fast single-agent
    # path. First team turn + genuinely complex follow-ups keep the pipeline.
    # Safe by default: any classifier failure leaves team=True. Disable with
    # AIFORGE_TEAM_AUTO_ROUTE=0.
    # NOTE: the actual classify call is deferred to the top of `_produce()`
    # (see below) — it's an LLM round-trip, and running it HERE, in the
    # synchronous request handler, delays the StreamingResponse from opening
    # at all: an unreachable/slow endpoint's retry+backoff chain (many
    # seconds) left the client with zero bytes and no ping, looking hung,
    # for a decision that only affects `_parallel_team` / `_events()` (both
    # only read once the background thread is already running).
    _auto_downgraded = False
    _parallel_team = False   # finalized in _produce(), once `team` is settled

    # Upgrade a freshly-named session to a concise MODEL-generated title,
    # CONCURRENTLY with the turn (a fast ~20-token call) so it neither blocks
    # the response nor lingers the stream. The client's post-turn session
    # refresh picks it up. Best-effort.
    if _fresh_title:
        def _gen_title():
            try:
                from aiforge_core.runtime import chat_store as _cs
                from aiforge_core.runtime import chat_title
                # Titling is a ~20-token throwaway — route it to the cheap
                # 'triage' role so it doesn't contend with the main turn on a
                # serial local endpoint (was the big session role).
                _t = chat_title.suggest_title(prompt, role="triage")
                if _t:
                    _cs.rename_session(session_id, _t)
            except Exception:  # noqa: BLE001 — titling must never break a run
                pass
        _spawn(_gen_title, name="gen-title")

    from aiforge_core.runtime import chat_cancel
    chat_cancel.start(session_id)
    # Steering is accepted in every mode: simple/plan drain mid-run steers in the
    # ReAct loop; parallel folds them into SPEC.md (stream_parallel_team) to guide
    # the remaining subtasks + reconcile. (Sequential team clears them at end.)
    from aiforge_core.runtime import chat_interject as _chat_interject
    _chat_interject.set_steerable(session_id, True)
    # Gap D — arm/disarm the pre-apply review gate for this run. Cleared on
    # chat_approve.finish() in every termination path (simple/parallel here,
    # team in chat_pipeline), so it never leaks into the next turn. The
    # actual set_review_edits() call is deferred to the top of `_produce()`
    # (needs the post-classify `team` value — see the auto-route note above).
    from aiforge_core.runtime import chat_approve as _chat_approve

    def _auto_checkpoint():
        # Snapshot the working dir at turn start so the user can roll back
        # this turn's edits. Best-effort; gated by env. Runs INSIDE _gen
        # (first, before streaming) so its git subprocesses don't delay the
        # StreamingResponse from opening.
        if os.environ.get("AIFORGE_CHAT_AUTO_CHECKPOINT", "1") in ("0", "false") \
                or team:
            return
        try:
            import datetime as _dt

            from aiforge_core.runtime import checkpoints
            _snap = checkpoints.snapshot(
                cwd, label=f"before: {prompt[:50]}",
                when=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            # Stamp this turn's snapshot onto the user message so edit-resend
            # can restore the workspace to exactly this turn's starting state.
            if isinstance(_snap, dict) and _snap.get("ok") and _snap.get("sha"):
                chat_store.set_message_checkpoint(_user_msg_id, _snap["sha"])
        except Exception:  # noqa: BLE001
            pass

    # Records which path the run actually took, so the persistence gate below
    # matches. ``driver`` is True ONLY once the sequential team ADK driver
    # (chat_pipeline) has been launched — it self-persists and owns the run's
    # lifetime. Every other path (simple/plan, parallel, best-of-N, OR a team
    # run that crashes in the pre-stream orchestrator before the driver starts)
    # persists + cleans up inline here.
    _path = {"parallel": False, "driver": False}

    def _events():
        # Built-in /help (or /commands): answer inline with the command listing
        # and finish — no model call, works with zero user command files.
        if _cmd_help_text is not None:
            yield {"type": "message", "text": _cmd_help_text}
            yield {"type": "done"}
            return
        # Builder mode (job|skill|workflow|rule): a focused, deterministic
        # interactive builder. Bypass the enhancer/team/plan machinery (the
        # enhancer would distort the clarifying Q&A) and run the single chat
        # agent with the task charter, which ends by calling its finalize tool.
        if body.builder:
            # NOTE: do NOT re-import run_chat_agent here — a local import inside
            # this generator makes the name LOCAL to the whole generator, so the
            # non-builder paths below (which don't run this branch) hit it
            # unbound → "UnboundLocalError: run_chat_agent". Use the closure from
            # the outer function's import.
            from aiforge_core.runtime.prompts_extended import builders as _bld
            if _bld.charter_for(body.builder):
                yield from run_chat_agent(history, cwd=cwd, role=role,
                                          session_id=session_id, mode="act",
                                          builder=body.builder)
                return
        # A user command expanded — a small notice so the user sees WHY their
        # "/deploy …" turned into a longer prompt (the agent runs on the
        # expanded template below, unchanged).
        if _cmd_expanded:
            yield {"type": "thought", "role": "command",
                   "text": f"Expanded /{_cmd_expanded} command template."}
        # Staleness auto-curation: a session bound to a jira/confluence
        # context folder (cwd = work/<kind>/<key>) re-verifies that context's
        # note when its updated_at crossed AIFORGE_NOTE_STALE_HOURS. The
        # pre-check is cheap and network-free; the actual curation re-fetches
        # the source, so it's HARD time-boxed (like the rule_capture pass
        # below) — a dead Jira must never stall the chat turn. FAILS OPEN.
        try:
            from aiforge_core.runtime import note_curator as _nc
            _stale_note = _nc.stale_note_path(cwd)
            if _stale_note:
                import concurrent.futures as _ncf
                _cres = None
                _nex = _ncf.ThreadPoolExecutor(max_workers=1)
                try:
                    _nbudget = float(os.environ.get(
                        "AIFORGE_NOTE_CURATE_BUDGET_S", "10"))
                    _cres = _nex.submit(_nc.curate_note,
                                        _stale_note).result(timeout=_nbudget)
                except Exception as _nexc:  # noqa: BLE001 — timeout/any → skip
                    _af_log.debug("note curation timed out/failed: %s", _nexc)
                finally:
                    _nex.shutdown(wait=False)
                # Visible only when something actually drifted — a silent
                # freshness bump shouldn't add chat noise.
                if _cres and _cres.get("ok") and _cres.get("changes"):
                    yield {"type": "thought", "role": "curator",
                           "text": ("Auto-curated stale note "
                                    f"{os.path.basename(_stale_note)}: "
                                    + "; ".join(_cres["changes"]))}
        except Exception as _nexc2:  # noqa: BLE001 — must never break a turn
            _af_log.debug("note staleness pass skipped: %s", _nexc2)
        # Rule / Memory / Feedback capture (deterministic, always-on) — runs
        # BEFORE any agent, independent of the agent's model, so a directive /
        # fact / correction stated in passing is captured + applied. FAILS OPEN:
        # any error here is swallowed and the normal run proceeds.
        try:
            from aiforge_core.runtime import rule_capture as _rc
            _repo = _rc.repo_key(cwd) or "repo"
            # PRE-FILTER: only spend an LLM classify when the message carries a
            # preference/directive cue. Ordinary turns ("hi", "fix the bug")
            # skip the classifier entirely — no per-turn LLM cost.
            if _rc.should_classify(prompt):
                import concurrent.futures as _cf

                def _capture_pass():
                    _c = _rc.classify(prompt, repo=_repo, session_id=session_id)
                    if _c.get("category") == "none":
                        return None
                    _s = _rc.store(_c, repo=_repo, session_id=session_id,
                                   repo_root=cwd)
                    # Recognition ONLY: detect a possible gate-disable request so
                    # the UI can OFFER an explicit opt-in. It sets NO flag.
                    _i = _rc.recognize_gate_intent(_c)
                    return _c, _s, _i

                # HARD wall-clock bound on the whole capture pass so a degraded
                # LLM can never stall the chat turn. Fully fail-open + fail-fast.
                _res = None
                _ex = _cf.ThreadPoolExecutor(max_workers=1)
                try:
                    _budget = float(os.environ.get("AIFORGE_CAPTURE_BUDGET_S", "6"))
                    _res = _ex.submit(_capture_pass).result(timeout=_budget)
                except Exception as _cexc:  # noqa: BLE001 — timeout/any → no capture
                    _af_log.debug("rule_capture pass timed out/failed: %s", _cexc)
                finally:
                    _ex.shutdown(wait=False)

                if _res is not None:
                    _cls, _stored, _intent = _res
                    _ev = {"type": "captured", "id": _stored.get("id"),
                           "category": _cls["category"], "scope": _cls["scope"],
                           "text": _cls.get("canonical", ""), "repo": _repo}
                    if _intent:
                        _ev["gate_intent"] = _intent     # UI offers opt-in pill
                    yield _ev
                    # PURE capture (no actionable task) → brief ack, skip the
                    # agent — UNLESS a deterministic actionable-intent backstop
                    # fires (e.g. "...and now fix the bug"): never drop a real
                    # task on the classifier's say-so.
                    if not _cls.get("task_present", True) \
                            and not _rc.looks_actionable(prompt):
                        yield {"type": "message",
                               "text": f"Got it — saved as {_cls['category']} "
                                       f"({_cls['scope']})."}
                        yield {"type": "done"}
                        return
        except Exception as _exc:  # noqa: BLE001 — capture must never break a turn
            _af_log.debug("rule_capture pre-agent pass failed: %s", _exc)
        # Team mode → full ADK agent flow (planner→…→learner) for complex
        # builds. Simple mode → single conversational agent for quick work.
        # Parallel team mode (AIFORGE_PARALLEL_SUBTASKS=1) → decompose then run
        # subtasks CONCURRENTLY in isolated worktrees with live status.
        from aiforge_core.runtime import parallel_subtasks as _pp
        # AUTO-ESCALATE: simple/plan modes on a multi-file BUILD request route
        # through the parallel pipeline — a single ReAct agent stalls on large
        # builds (one huge-context call, no decomposition). Gated + heuristic so
        # chit-chat / small edits still use the fast single-agent path.
        # ── TASK-TYPE ROUTING — see aiforge_core.runtime.chat_router ──────────
        # The heavy decision (which path handles this request) is a PURE function
        # there; here we only gather its side-effecting inputs and dispatch:
        #   • _psub_on   parallel capability (raw — escalation can fire off-team);
        #   • _greenfield  is this a fresh/empty tree?;
        #   • _fresh     NOT a follow-up (only fresh turns pay the LLM classify);
        #   • _cat       the LLM class (chat|tracker|doc_analysis|code_build|
        #                code_edit) or None → chat_router falls back to regex;
        #   • _team_approvals  Pipeline-approvals ON → force the gated sequential
        #                pipeline (the parallel path can't gate — J).
        _psub_on = False
        try:
            _psub_on = _pp.enabled()
        except Exception:  # noqa: BLE001
            _psub_on = _parallel_team
        _greenfield = True
        try:
            _greenfield = _pp._is_greenfield(cwd)
        except Exception:  # noqa: BLE001
            _greenfield = True
        try:
            from aiforge_core.runtime import turn_router as _tr2
            _fresh = not _tr2.is_followup(history)
        except Exception:  # noqa: BLE001
            _fresh = True
        _cat = None
        if _fresh:
            try:
                from aiforge_core.runtime import task_router as _tr
                _cat = _tr.classify_task(prompt, history=history, cwd=cwd)
            except Exception:  # noqa: BLE001 — never break routing on the classifier
                _cat = None
        _team_approvals = bool(team)   # fail safe → gated sequential
        try:
            from aiforge_core.config import approval_settings as _aps
            _team_approvals = bool(team and _aps.required("team"))
        except Exception:  # noqa: BLE001
            pass
        from aiforge_core.runtime import chat_router as _cr
        _rd = _cr.decide(
            prompt, agent_mode=agent_mode, team=team, psub_on=_psub_on,
            greenfield=_greenfield, fresh=_fresh, cat=_cat,
            team_approvals=_team_approvals,
            auto_escalate=os.environ.get("AIFORGE_AUTO_ESCALATE", "1")
            not in ("0", "false"))
        _doc_task = _rd.doc_task
        _is_build_task = _rd.is_build_task
        _build_escalate = _rd.build_escalate
        _route_pipeline = _rd.route_pipeline
        if _rd.notice:
            yield {"type": "thought", "role": "router", "text": _rd.notice}
        if _doc_task:
            # Multi-repo analysis fans OUT (one read-only explore agent per repo,
            # in parallel, then synthesize a draft). A single-repo/topic analysis
            # or a plain doc task stays on the single research agent below.
            try:
                from aiforge_core.runtime import analysis_pipeline as _ap
                _fan, _ana_repos, _ana_topics = _ap.should_fan_out(prompt, cwd)
            except Exception:  # noqa: BLE001 — never break routing on the probe
                _fan, _ana_repos, _ana_topics = (False, [], [])
            # No _psub_on gate: analysis fan-out is READ-ONLY + bounded, and its
            # concurrency already respects AIFORGE_PARALLEL_SUBTASKS_MAX (=1 →
            # sequential). Gating on _psub_on left a single agent seeing only
            # cwd, silently dropping the other repos.
            if _fan:
                yield from _ap.stream_analysis_team(
                    prompt, cwd=cwd, session_id=session_id,
                    repos=_ana_repos, topics=_ana_topics)
                return
            # Single repo but the task names MANY real files → PLAN it into
            # bounded read-only groups (discover→batch-read→synthesize), one
            # explore agent each. A flat many-file analysis is exactly what a
            # local model can't track on one agent; planning keeps every step
            # inside its ceiling. Disable with AIFORGE_ANALYSIS_MIN_FILES=999.
            try:
                _plan, _ana_groups, _ana_topics2 = _ap.plan_single_repo(prompt, cwd)
            except Exception:  # noqa: BLE001 — never break routing on the probe
                _plan, _ana_groups, _ana_topics2 = (False, [], [])
            if _plan:
                _nfiles = sum(len(g.get("files") or []) for g in _ana_groups)
                yield {"type": "thought", "role": "router",
                       "text": (f"Doc/analysis on one repo spanning {_nfiles} "
                                f"files — planning into {len(_ana_groups)} bounded "
                                "read-only groups (discover → batch-read → "
                                "synthesize), one explore agent each, so a local "
                                "model never faces a flat multi-file sweep.")}
                yield from _ap.stream_analysis_planned(
                    prompt, cwd=cwd, session_id=session_id,
                    groups=_ana_groups, topics=_ana_topics2)
                return
            yield {"type": "thought", "role": "router",
                   "text": "Doc/analysis task (analysis or a doc/Confluence "
                           "deliverable) — routing to the single research agent, "
                           "NOT the code build pipeline. No file tree, tests, or "
                           "PR; the output is the analysis/document (draft)."}
        # (the team not-route_pipeline notice — approvals-sequential vs in-place
        # edit — is emitted above via chat_router's _rd.notice.)
        # Review-edits is a simple/plan-only feature (forced on there). Team /
        # parallel / best-of-N runners run the full pipeline and don't hold
        # edits — left as-is by design, no notice (avoids per-run noise).
        if _route_pipeline:
            # Orchestrator (layer 1) = 3 agents: enhancer → architect → planner.
            # SCOPE the enhancer's memory recall to THIS session's repo — without
            # a repo, unified_query runs its repo-agnostic sources (prior chat
            # sessions + global vector) and an UNRELATED task bleeds into the
            # build spec (a "mathx" build decomposed into game/storage). The
            # contamination guard in unified_query only fires with a repo set.
            from aiforge_core.runtime.chat_agent import _chat_repo_key as _crk
            _pl_repo = _crk(cwd)
            _spec = _pp._enhance(prompt, history=history, cwd=cwd, repo=_pl_repo)  # 1. clean spec
            _files = _pp._architect(_spec, cwd=cwd)  # 2. design file structure
            _subs = _pp._plan_files(_files) if len(_files) >= 2 \
                else _pp._decompose(_spec)          # 3. split (per file, or plan)
            if len(_subs) >= 2:
                _path["parallel"] = True
                yield from _pp.stream_parallel_team(_spec, cwd=cwd, subtasks=_subs,
                                                    enhanced=True, session_id=session_id)
                # stream_parallel_team emits no terminal `done`; synthesize one
                # so a UI waiting on `done` doesn't hang (exactly one — the
                # exception path in _gen only fires on error).
                yield {"type": "done"}
                return
            # Couldn't split into ≥2 distinct files → it's really ONE task.
            # STILL write SPEC.md (user requirement: every pipeline-routed run
            # tracks against a spec): only stream_parallel_team used to write
            # it, so the <2-subtask fallbacks (best-of-N / sequential / single
            # agent) ran spec-less — 'sometimes there is no SPEC.md'.
            try:
                _spec_doc = _pp._render_spec_md(_spec, _subs)
                with open(os.path.join(cwd, "SPEC.md"), "w",
                          encoding="utf-8") as _fh:
                    _fh.write(_spec_doc)
                yield {"type": "thought", "role": "planner",
                       "text": "Wrote SPEC.md (single-task plan) — the run "
                               "builds and is verified against it."}
            except Exception as _sexc:  # noqa: BLE001 — visible, never silent
                yield {"type": "thought", "role": "planner",
                       "text": f"⚠ SPEC.md write failed: {_sexc}"}
            # Best-of-N (Gap C, opt-in): when AIFORGE_BEST_OF_N is set, run the
            # single task N independent times in isolated worktrees, grade each,
            # keep the best. Otherwise fall back to the sequential team pipeline
            # so the user always gets a result. Default flow (flag unset) is
            # unchanged.
            if os.environ.get("AIFORGE_BEST_OF_N"):
                from aiforge_core.runtime import best_of_n as _bon
                _af_log.info("parallel decompose <2 subtasks — best-of-N route")
                _path["parallel"] = True
                yield from _bon.stream_best_of_n(_spec, cwd,
                                                 session_id=session_id)
                # stream_best_of_n emits no terminal `done`; synthesize one so a
                # UI waiting on `done` doesn't hang (exactly one).
                yield {"type": "done"}
                return
            _af_log.info("parallel decompose <2 subtasks — sequential fallback")
        if team and not _doc_task:
            # Sequential team pipeline already has its own ADK enhancer agent;
            # don't double-enhance here. Mark the driver launched ONLY here —
            # so a crash in the parallel pre-steps above (which never reach this
            # line) still persists + cleans up inline in _gen's finally.
            # A DOC/ANALYSIS task falls through to the single research agent
            # below (not this code pipeline), even in team mode.
            _path["driver"] = True
            yield from stream_chat_pipeline(prompt, cwd=cwd, session_id=session_id,
                                            history=history, started_at=_turn_t0)
            return
        # SIMPLE and PLAN modes — the Enhancer is MANDATORY on the FIRST turn
        # of a session (fresh context, referents to resolve, no memory pulled
        # yet). On a FOLLOW-UP, re-running the enhancer (an LLM round-trip
        # that also fires the memory recall inside `_enhance`) on every single
        # message is wasted latency for the common case ("fix that", "add a
        # test") — so reuse the same cheap classify already used to
        # auto-downgrade team turns (turn_router.classify) and skip the
        # enhancer when this follow-up is small. Any classify failure (or the
        # first turn, or a build-escalate spec already in flight) keeps the
        # enhancer mandatory — safe default, never silently under-enhance.
        _skip_enhance = _auto_downgraded and not _route_pipeline
        if not _skip_enhance and not _route_pipeline:
            try:
                from aiforge_core.runtime import turn_router as _tr2
                # Skip the enhancer ONLY for a SIMPLE follow-up ("fix that",
                # "add a test") — there the history-fold below carries the
                # context and a second LLM round-trip + memory recall is wasted
                # latency. A COMPLEX follow-up ("no, use postgres instead") or a
                # classify FAILURE keeps the enhancer MANDATORY — it resolves the
                # referent against the prior turns instead of running on the raw
                # prompt (skipping it there under-serves the request). A genuine
                # multi-file build follow-up also enhances.
                if _tr2.is_followup(history) and not _is_build_task:
                    try:
                        _cls = _tr2.classify(prompt, history=history)
                    except Exception:  # noqa: BLE001 — classify blew up → enhance
                        _cls = "complex"
                    if _cls == "simple":
                        _skip_enhance = True
            except Exception as _sexc:  # noqa: BLE001 — never block a turn
                _af_log.debug("enhancer skip-check failed: %s", _sexc)
        if _auto_downgraded:
            yield {"type": "thought", "role": "router",
                   "text": "Small follow-up — handling directly (skipped the "
                           "full pipeline for speed)."}
        if _skip_enhance:
            _enriched = prompt
        else:
            yield {"type": "thought", "role": "enhancer",
                   "text": "Enhancing request + gathering context…"}
            # Fold `history` INTO the spec (restores referent resolution: a
            # context-dependent follow-up like "no, use postgres instead" or
            # "fix that bug" must be resolved against the prior turns, else
            # the enhancer fabricates a context-free spec that REPLACES the
            # user's words).
            from aiforge_core.runtime.chat_agent import _chat_repo_key as _crk2
            _enriched = _pp._enhance(prompt, history=history, cwd=cwd,
                                     repo=_crk2(cwd))   # scope recall (anti-contamination)
        # Replace the LAST user turn's content with the enriched spec, keeping
        # every prior turn intact. Trimming the recent turns (an earlier "avoid
        # the double-fold" attempt) broke claude_local's user/assistant
        # alternation and dropped context when `_enhance` no-ops on a trivial
        # follow-up ("yes"/"no"). The residual double-fold (recent turns appear
        # raw AND folded into the spec) is benign token redundancy, not semantic
        # harm; alternation stays intact and no turn is ever dropped.
        _enriched_history = [dict(m) for m in history]
        for _m in reversed(_enriched_history):
            if _m.get("role") == "user":
                # AUGMENT, don't replace: keep the user's verbatim words and
                # attach the enhancer's interpretation as a clearly-labelled
                # block the model can cross-check. A distorted/hallucinated
                # enhancement no longer silently becomes the request (the raw
                # ask is right there). If _enhance no-ops, skip the block.
                _raw = (_m.get("content") or "").strip()
                if _enriched and _enriched.strip() and _enriched.strip() != _raw:
                    _m["content"] = (
                        f"{_raw}\n\n---\n[Interpreted request — a context-enriched "
                        f"restatement; if it conflicts with my words above, my "
                        f"words win:]\n{_enriched}")
                # DRAFT-ONLY for a doc/analysis task: the deliverable is the
                # written analysis/document as markdown in the final answer. Do
                # NOT publish to Confluence/Jira (no confluence_create/update,
                # jira_create) unless the user EXPLICITLY says publish/post — a
                # wrong or premature write to the team wiki is not recoverable.
                if _doc_task:
                    # EXPLICIT publish intent only — 'post the findings' / a
                    # 'post-the-fact review' must NOT flip publishing on. Default
                    # to draft (a wrong write to the team wiki is unrecoverable).
                    _pub = bool(__import__("re").search(
                        r"\b(publish it|publish the (page|doc|report)|"
                        r"go ahead and (publish|post)|actually (publish|create "
                        r"the (page|confluence))|post it to confluence)\b",
                        (prompt or "").lower()))
                    if not _pub:
                        _m["content"] += (
                            "\n\n---\n[Deliverable = DRAFT ONLY. Produce the "
                            "analysis/document as markdown in your final answer. "
                            "Do NOT publish — do not call confluence_create/"
                            "confluence_update/jira_create. Read tools (repos, "
                            "web, existing pages) are fine. I will review and "
                            "post it myself.]")
                break
        if agent_mode == "plan":
            _subs = _pp._decompose(_enriched)       # Planner
            if _subs:
                # Plan mode shows a STATIC plan it never executes — mark them
                # "planned" (not "pending") so the UI doesn't render them as
                # stuck-forever pending-execution rows.
                yield {"type": "subtasks", "items": [
                    {"slug": s.get("slug") or f"sub-{i+1}",
                     "goal": s.get("goal") or s.get("title") or "",
                     "status": "planned"}
                    for i, s in enumerate(_subs)]}
            # Plan→approve→execute (Gap B): hand the approved spec to the UI so
            # the user can one-click "Approve & Execute" — which re-sends this
            # enriched spec as a TEAM run. Persisted so the button survives a
            # reload until the plan is acted on. Emit plan_ready BEFORE the
            # agent's terminal `done` reaches the client (hold the `done`, yield
            # plan_ready, then release `done`) so the UI sees the plan, not a
            # finished turn with no plan.
            _pending_done = None
            for _ev in run_chat_agent(_enriched_history, cwd=cwd, role=role,
                                      session_id=session_id, mode="plan",
                                      max_steps=_quick_step_cap(body.quick)):
                if _ev.get("type") == "done":
                    _pending_done = _ev
                    continue
                yield _ev
            yield {"type": "plan_ready", "spec": _enriched}
            if _pending_done is not None:
                yield _pending_done
            return
        # Baseline commit so we can show a Changes diff after the single-agent run
        # (simple mode edits the working tree; the pipeline shows its own Changes).
        # A fresh chat workspace is NOT a git repo — the old rev-parse/empty-tree
        # dance then left _simple_sha unusable (git diff needs a real repo), so the
        # Changes view silently vanished. _ensure_git_workspace git-inits + makes a
        # committed baseline (no-op when cwd is already a repo, e.g. a pinned user
        # project), so HEAD is ALWAYS a valid baseline to diff the run against.
        # CRITICAL: commit the CURRENT working-tree state into the baseline so
        # this turn's Changes diff + the "did it write source?" gate reflect ONLY
        # what THIS turn does. A reused chat/ticket workspace (e.g. session-1)
        # carries a previous task's uncommitted files; without this snapshot,
        # `git status` reports THEM, so a no-code Jira/Q&A turn wrongly triggers
        # the build/integration pipeline on stale files and the Changes view
        # shows the previous ticket's edits.
        _simple_sha = ""
        # A jira/confluence context folder holds a generated dossier + notes, NOT
        # code — code work for a ticket lives in the resolved repo, never here. So
        # never show a Changes view for it: a plain READ writes ticket.md /
        # dossier.md / attachments/ (+ the .gitignore) and would otherwise report
        # "N files changed". Skip the worktree baseline + the changes event for
        # ANY such context, even one already git-inited by an earlier turn.
        # Real repos / repo-context / session scratch still track normally.
        _skip_worktree = False
        try:
            from aiforge_core.runtime import work_context as _wc0
            _ctx0 = _wc0.context_for_path(cwd)
            if _ctx0 and _ctx0[0] in ("jira", "confluence", "web"):
                _skip_worktree = True
                _af_log.info("chat: no Changes view for %s dossier folder %s "
                             "(read-only context)", _ctx0[0], cwd)
        except Exception:  # noqa: BLE001
            pass
        if not _skip_worktree:
            try:
                from aiforge_core.runtime.parallel_subtasks import _commit_turn_baseline
                _simple_sha = _commit_turn_baseline(cwd)
            except Exception:  # noqa: BLE001
                _simple_sha = ""
        # A doc/analysis task is READ-ONLY: force analyze mode so the single
        # agent (like the fan-out explores) can't write/patch/bash in the user's
        # real repo — it produces the analysis/document as its answer. Otherwise
        # a "analyze X and write a report" turn ran writable and could mutate the
        # repo + trigger the post-run build. Non-doc turns keep their mode.
        _single_mode = "analyze" if _doc_task and agent_mode != "plan" else agent_mode
        _turn_awaiting = False
        for _ev in run_chat_agent(_enriched_history, cwd=cwd, role=role,
                                  session_id=session_id, mode=_single_mode,
                                  max_steps=_quick_step_cap(body.quick)):
            if _ev.get("type") == "message" and _ev.get("awaiting_input"):
                _turn_awaiting = True
            yield _ev
        # A turn that ended AWAITING user input — e.g. a REJECT ("tell me what to
        # do instead") or an ASK — must NOT fall into the post-run integration
        # build below: on a turn that had an earlier APPLIED edit, _wrote_source()
        # is True and the build fires AFTER the reject, holds the is_running slot
        # (run.finish() is in _produce's finally), and 409-blocks the user's very
        # next (resume) message. Pointless work on a paused turn — end here.
        if _turn_awaiting:
            return
        # Read-only / analysis query ("analyze/explain/how does X work") → the user
        # wants an EXPLANATION, not a build. Don't run the integration-check +
        # self-heal (which would build/test an existing repo and report a failure
        # instead of the analysis).
        def _looks_like_analysis(p: str) -> bool:
            import re as _re
            p = (p or "").lower()
            ask = _re.search(r"\b(analy[sz]e|explain|describe|summar[iy][sz]e|"
                             r"review|understand|audit|document|investigate|trace|"
                             r"walk\s*(me)?\s*through|how\s+(does|do|is|are)|"
                             r"what\s+(does|is|are)|why\s+(does|is|are)|where\s+"
                             r"(is|are)|tell me about|show me how)\b", p)
            change = _re.search(r"\b(fix|create|build|implement|add|write|refactor|"
                                r"rename|delete|remove|update|generat|make|"
                                r"modify|patch|scaffold)\b", p)
            return bool(ask and not change)

        _readonly = _looks_like_analysis(prompt)

        def _wrote_source() -> bool:
            """True only if this turn CREATED/MODIFIED a source file — the signal
            there's something to build+test. A JIRA/Confluence/Q&A/chat/analysis
            turn touches no source, so the integration-check (+ its hardcoded
            python fallback steps) must NOT run for it."""
            exts = (".py", ".java", ".go", ".js", ".mjs", ".ts", ".tsx", ".c",
                    ".cc", ".cpp", ".h", ".hpp", ".rs", ".rb", ".php", ".cs",
                    ".kt", ".swift", ".scala", ".sh")
            try:
                import subprocess as _sp
                _r = _sp.run(["git", "-C", cwd, "status", "--porcelain"],
                             capture_output=True, text=True, timeout=10)
                if _r.returncode == 0:
                    # git ran cleanly → the working tree IS the answer. Because a
                    # pre-turn baseline commit was taken, this reflects ONLY this
                    # turn's writes: source touched → True, otherwise (empty tree
                    # OR non-source changes) → False. Do NOT fall through to the
                    # process-global touched_paths(), which can hold a PRIOR turn's
                    # path and would re-trigger the build on a no-code turn.
                    return any(ln[3:].strip().endswith(exts)
                               for ln in (_r.stdout or "").splitlines() if ln.strip())
            except Exception:  # noqa: BLE001 — git missing / timeout
                pass
            # Fallback ONLY when git is unusable (not a repo): best-effort.
            try:
                from aiforge_core.runtime.doer_tools import touched_paths
                return any(str(p).endswith(exts) for p in touched_paths())
            except Exception:  # noqa: BLE001
                return False

        # Simple/act mode: after the agent finishes, COMPILE + run the project's
        # tests and report — ONLY when this turn actually wrote source code. Skips
        # plan mode, read-only analysis, and every non-code task (JIRA/Confluence/
        # Q&A/chat). Best-effort; env-gated off with AIFORGE_CHAT_INTEGRATION_TEST=0.
        # PROPORTIONALITY: only run the (heavy) build+test+self-heal when there's
        # something to verify — a detectable build/test stack. A doc/config/tiny
        # edit in a repo with no tests + no build system gets the Changes diff, not
        # a pointless build cycle. AIFORGE_CHAT_INTEGRATION_TEST=0 disables entirely.
        def _worth_verifying() -> bool:
            try:
                from aiforge_core.runtime.tools.project_runner import (
                    _has_tests,
                    detect,
                )
                stacks = (detect(cwd) or {}).get("stacks") or []
                if stacks and _has_tests(cwd, stacks):
                    return True
                # bare python (no marker) but with test files → still worth it
                import glob
                return bool(glob.glob(os.path.join(cwd, "**", "test_*.py"),
                                      recursive=True)
                            or glob.glob(os.path.join(cwd, "**", "*_test.py"),
                                         recursive=True))
            except Exception:  # noqa: BLE001
                return True   # unsure → keep the old behaviour (verify)
        if agent_mode != "plan" and not _readonly and _wrote_source() \
                and os.environ.get(
                    "AIFORGE_CHAT_INTEGRATION_TEST", "1") not in ("0", "false") \
                and _worth_verifying():
            try:
                from aiforge_core.runtime.parallel_subtasks import (
                    _reconcile_integration,
                )
                yield {"type": "thought", "role": "verifier",
                       "text": "Building + running integration tests…"}
                # Same self-heal as the pipeline: build+test, and if it fails,
                # rewrite the offending files until green (bounded), then report.
                _ires: dict = {}
                yield from _reconcile_integration(cwd, _ires)
                _rep = _ires.get("rep") or {}
                # Only show the integration report when a build/test ACTUALLY ran
                # (ok True/False). ok=None = "no build markers / no toolchain" — a
                # simple file edit with no tests — so DON'T dump the "build & test
                # it yourself (python)" boilerplate as the answer; the Changes diff
                # below is the useful output.
                if _rep.get("md") and _rep.get("ok") is not None:
                    # supplementary=True: render the build report but DON'T let it
                    # replace the agent's own answer as the persisted final_text.
                    yield {"type": "message", "text": _rep["md"],
                           "role": "verifier", "supplementary": True}
            except Exception as _iexc:  # noqa: BLE001 — never break the turn
                _af_log.debug("integration report skipped: %s", _iexc)
        # SHOW CHANGES (simple mode too) — a clean PR-style diff of what the single
        # agent edited, same view as the pipeline. Working-tree diff (uncommitted).
        # Gated on `not _readonly` ONLY (NOT _wrote_source, which lists code
        # extensions) so a doc/config-only edit (README, yaml, json, Dockerfile)
        # still shows its diff. _emit_changes self-guards on an empty diff, so a
        # pure Q&A turn that wrote nothing simply emits no changes event.
        if _simple_sha and not _readonly:
            try:
                from aiforge_core.runtime.parallel_subtasks import _emit_changes
                yield from _emit_changes(cwd, _simple_sha, include_worktree=True)
            except Exception as _cx:  # noqa: BLE001
                _af_log.debug("simple changes diff skipped: %s", _cx)

    # The PRODUCER runs on a background daemon thread and publishes every event
    # into the per-session run registry (chat_runs). It NO LONGER yields to the
    # HTTP response, so a client that navigates away (aborting the fetch) can't
    # kill the run — the thread runs to completion and persists the full turn.
    # The HTTP response (and any later /attach) just SUBSCRIBES and tails the
    # buffer. This is the same survive-the-disconnect pattern team mode already
    # used internally, now applied to every mode. (chat_runs imported above for
    # the is_running concurrency guard.)
    run = chat_runs.start(session_id)

    def _produce():
        nonlocal team, _auto_downgraded, _parallel_team
        _PRODUCE_SEM.acquire()   # bounded — block until a producer slot frees
        # Bind this producer thread to the session so LLM tracing (Langfuse
        # sessions/scores) tags every generation with the run it belongs to.
        # Covers ALL modes here (simple/plan run inline in this thread; team's
        # _drive re-sets the env in its own thread). Env for cross-thread /
        # subprocess reach; contextvar for concurrency-correct in-thread reads.
        os.environ["AIFORGE_CURRENT_SESSION"] = str(session_id)
        from aiforge_core.runtime import request_context as _reqctx
        _sess_token = _reqctx.set_session_id(session_id)
        # Bind the repo root to the turn's cwd so the codegraph gate (which some
        # Doer-side call sites resolve via request_context.get_repo_root() with
        # NO cwd) sees the SAME repo the tools run against. Without this, simple
        # chat left the repo root unset and those sites fell back to "." (the
        # AIForge process dir), so codegraph was mis-gated off the wrong folder.
        _repo_token = _reqctx.set_repo_root(cwd)
        # Auto-route classify + its dependents, run HERE (already off the
        # response-open path — see the note where `team`/`_parallel_team`
        # were declared above) rather than in the synchronous request
        # handler, so a slow/unreachable classify LLM never delays the
        # StreamingResponse itself.
        if team:
            try:
                from aiforge_core.runtime import turn_router as _tr
                if _tr.should_downgrade_team(prompt, history, cwd):
                    team = False
                    _auto_downgraded = True
                    _af_log.info("chat: team turn auto-downgraded to simple "
                                 "(small follow-up) session=%s", session_id)
            except Exception as _rexc:  # noqa: BLE001 — routing must never block a turn
                _af_log.debug("turn_router skipped: %s", _rexc)
        _parallel_team = team and _psub.enabled()
        # Review-edits gate: OFF by default — file writes/patches auto-apply,
        # no per-edit Approve/Reject prompt (the operator asked for no file-
        # permission prompts). Re-enable per-request via body.review_edits, or
        # globally with AIFORGE_CHAT_REVIEW_EDITS=1. Team/parallel mode never
        # holds edits regardless (the full pipeline runs unattended).
        _review_env = os.environ.get(
            "AIFORGE_CHAT_REVIEW_EDITS", "0") in ("1", "true", "yes", "on")
        _chat_approve.set_review_edits(
            session_id, (bool(body.review_edits) or _review_env) and not team)
        # Record the EFFECTIVE run mode (after any team→simple downgrade) so the
        # tool gate can honor the per-mode approval Settings toggle.
        _eff_mode = "team" if team else ("plan" if agent_mode == "plan" else "simple")
        _chat_approve.set_mode(session_id, _eff_mode)
        steps: list[dict] = []
        final_text = ""
        awaiting = False   # turn ended with a question / pause, not an outcome
        _subtasks: list[dict] = []   # live subtask panel state, persisted so it
        #                              survives a navigate-away / reload
        # Mirror chat activity into the observability NDJSON so the Logs page
        # shows live runs (the page tails orchestrator-<role>.ndjson).
        try:
            from aiforge_core.observability.logging import emit, get_logger
            # ONE shared "chat" logger (so the Logs "chat" tab tails one file).
            # Don't stash a per-session ticket on the process-wide singleton —
            # concurrent sessions would clobber it; stamp `session` per emit below.
            _clog = get_logger("chat")
        except Exception:  # noqa: BLE001
            _clog = None
            emit = None  # type: ignore
        _auto_checkpoint()   # snapshot first (off the response-open path)
        # Terminal subtask statuses — a cancelled run coerces any non-terminal
        # row to "failed" so the persisted/reloaded panel never shows a row
        # stuck pending/running after a Stop.
        # "planned" is a settled, never-executed plan-mode state — NOT in-flight,
        # so a cancel must not flip it to "failed".
        _TERMINAL = {"done", "failed", "skipped", "won", "planned"}
        emitted_done = False   # forwarded a terminal `done` yet?
        try:
            for ev in _events():
                # Never surface leaked protocol scaffolding to the user. A local
                # model in native-FC mode sometimes emits a fumbled tool call as
                # plain content ("ARGS_JSON: {}", a bare "ACTION:") — that reached
                # the chat as a message bubble. Strip marker-only noise from any
                # non-ASK message text; if nothing real remains, drop the event so
                # it neither streams nor persists as the answer. This is the single
                # choke point for both the client publish and the persisted
                # final_text below.
                if ev.get("type") == "message" and not ev.get("awaiting_input"):
                    from aiforge_core.runtime.chat_agent._prompt import (
                        _strip_protocol_noise,
                    )
                    _clean = _strip_protocol_noise(ev.get("text") or "")
                    if not _clean:
                        continue
                    if _clean != ev.get("text"):
                        ev = {**ev, "text": _clean}
                # Stamp per-turn wall-clock on the terminal event so the UI shows
                # time-taken for EVERY turn in ALL three modes (simple/plan/team),
                # server-authoritative (the client timer is live-only).
                if ev.get("type") == "done" and "elapsed_s" not in ev:
                    ev = {**ev, "elapsed_s": round(_time.time() - _turn_t0, 2),
                          "mode": _turn_mode}
                if _clog is not None and emit is not None and \
                        ev.get("type") in ("thought", "tool", "message", "error"):
                    try:
                        emit(_clog, ev["type"], session=session_id, name=ev.get("name"),
                             text=(ev.get("text") or "")[:200],
                             tool_ok=(ev.get("result") or {}).get("ok") if isinstance(ev.get("result"), dict) else None)
                    except Exception:  # noqa: BLE001
                        pass
                if ev.get("type") == "message" and not ev.get("supplementary"):
                    # A supplementary message (e.g. the build/integration report)
                    # renders but must NOT replace the agent's own answer as the
                    # persisted final_text — persist it as a step instead.
                    final_text = ev.get("text", "")
                    awaiting = bool(ev.get("awaiting_input"))
                elif ev.get("type") == "message" and ev.get("supplementary") or ev.get("type") in ("thought", "tool", "error", "changes"):
                    steps.append(ev)
                elif ev.get("type") == "subtasks":
                    _subtasks = list(ev.get("items") or [])
                elif ev.get("type") == "subtask_update":
                    for _s in _subtasks:
                        if _s.get("slug") == ev.get("slug"):
                            _s["status"] = ev.get("status")
                elif ev.get("type") == "plan_ready":
                    # Persist the approvable plan (Gap B) so the "Approve &
                    # Execute" button survives a reload.
                    steps.append(ev)
                elif ev.get("type") == "captured":
                    # Persist the capture pill so the inline "Saved RULE · scope"
                    # note (change-scope / undo) survives a reload.
                    steps.append(ev)
                if ev.get("type") == "done":
                    emitted_done = True
                run.publish(ev)
                if chat_cancel.is_cancelled(session_id):
                    # Stop pressed mid-stream (parallel / best-of-N break out
                    # BEFORE their synthesized `done`): reconcile any in-flight
                    # subtask row to a terminal state so nothing reloads stuck.
                    for _s in _subtasks:
                        if _s.get("status") not in _TERMINAL:
                            _s["status"] = "failed"
                    break
            # FINAL request count. The in-loop `usage` events fire BEFORE each
            # model call, so the last one always under-reports by at least the
            # answer's own call (plus any retry it needed). Emit the settled
            # numbers once the run is over, so the count the user is left
            # looking at is the true one.
            try:
                from aiforge_core.llm import call_meter as _meter
                _calls = _meter.snapshot(session_id)
                run.publish({"type": "usage", "llm_turn": _calls["turn"],
                             "llm_session": _calls["session"],
                             "llm_per_min": _calls["per_minute"],
                             "final": True})
            except Exception:  # noqa: BLE001 — metering must never break a turn
                pass
            # Persist the final subtask panel as a step so reload restores it.
            if _subtasks:
                steps.insert(0, {"type": "subtasks", "items": _subtasks})
            # The UI unblocks on a terminal `done`. A cancelled parallel/
            # best-of-N run breaks before its synthesized `done`, so guarantee
            # exactly one here when none was forwarded (non-cancel paths already
            # emit their own — don't double-emit).
            if not emitted_done:
                run.publish({"type": "done"})
                emitted_done = True
        except Exception as exc:  # noqa: BLE001
            run.publish({"type": "error", "text": str(exc)})
            run.publish({"type": "done"})
        finally:
            # Capture cancellation BEFORE finishing the token (finish pops
            # it, after which is_cancelled always reads False).
            cancelled = chat_cancel.is_cancelled(session_id)
            # Emit one turn-outcome score per run so the Langfuse Scores view
            # populates (0.0 stopped, 1.0 completed), tagged to this session.
            # Side-channel: soft-fails, never affects the turn. Runs for every
            # mode (this finally is hit inline for simple/plan/parallel and for
            # a team run whether or not the ADK driver launched).
            try:
                from aiforge_core.integrations import langfuse_adapter as _lf
                if _lf.enabled():
                    _lf.record_score(
                        name="turn_completed",
                        value=0.0 if cancelled else 1.0,
                        session_id=session_id,
                        comment="cancelled" if cancelled else "completed",
                        metadata={"mode": _turn_mode})
            except Exception:  # noqa: BLE001 — tracing must never break a turn
                pass
            try:
                _reqctx.reset_session_id(_sess_token)
            except Exception:  # noqa: BLE001
                pass
            try:
                _reqctx.reset_repo_root(_repo_token)
            except Exception:  # noqa: BLE001
                pass
            # TEAM mode: the background driver owns the run's lifetime AND its
            # persistence (chat_pipeline._drive) — it survives a client
            # disconnect and holds the real final answer, so we must NOT
            # persist a partial here (and finishing the token here would
            # orphan a still-running ADK run on Stop). SIMPLE mode runs inline
            # in this producer thread, so finish + persist here.
            # Parallel team mode is a self-contained generator (not the
            # background ADK driver), so persist it inline like simple mode.
            # The sequential fallback uses the team driver, which self-persists.
            # Gate on whether that driver actually LAUNCHED — a team run that
            # crashes in the pre-stream orchestrator (enhance/architect/
            # decompose) never starts the driver, so it must clean up here too.
            if not _path["driver"]:
                chat_cancel.finish(session_id)
                from aiforge_core.runtime import chat_interject
                chat_interject.clear(session_id)   # no stale steers next turn
                from aiforge_core.runtime import chat_approve, chat_persist
                chat_approve.finish(session_id)
                chat_persist.persist_turn(
                    session_id=session_id, cwd=cwd, prompt=prompt,
                    final_text=final_text, steps=steps,
                    team=(team or _path["parallel"]),
                    cancelled=cancelled, awaiting=awaiting,
                    mode=_turn_mode, duration_s=_time.time() - _turn_t0)
                # Single-chat (simple/plan) memory writeback. The team
                # pipeline runs a Learner node + memory callbacks itself;
                # the inline simple/plan path never did, so chat work never
                # reached long-term memory. Distil + persist durable facts
                # on a daemon thread (off the response path). Skip cancelled
                # turns and the parallel-team path (its own runners cover it).
                if not cancelled and not team and not _path["parallel"]:
                    def _chat_learn():
                        try:
                            from aiforge_core.runtime import chat_learner
                            from aiforge_core.runtime.chat_agent import _chat_repo_key
                            # Same key resolution as RECALL (_chat_repo_key,
                            # git-toplevel basename) — the old bare repo_key(cwd)
                            # filed subdir-pinned sessions under the subdir while
                            # recall read the repo root, so facts were never found.
                            _repo = _chat_repo_key(cwd)
                            # PREFERENCE FIRST — a message with a preference cue
                            # ("use X as default", "from now on…") is UPSERTED by
                            # subject and owns the turn: skip the fact-distiller
                            # for it, so we don't pay a SECOND LLM call and write
                            # the same sentence twice (a pref: unit AND a learning
                            # unit). Ordinary turns still distil facts.
                            from aiforge_core.runtime import preference_capture
                            _pc = preference_capture.capture(
                                prompt, repo=_repo, session_id=session_id)
                            # Run the learner on EVERY turn (even when a
                            # preference was captured) — it distils the OTHER
                            # signal: technical learnings, project-structure
                            # findings (folder layout, patterns, build/test cmd),
                            # and any durable user intent the pref-subject capture
                            # didn't own. It dedups against memory context, so it
                            # won't re-emit the pref. Comprehensive capture is the
                            # point — every message + every discovery is considered.
                            _lr = chat_learner.learn_from_chat(
                                prompt=prompt, final_text=final_text,
                                steps=steps, repo=_repo, session_id=session_id)
                            # Don't SILENTLY drop a failed persist — a "remember X"
                            # that fails to store is real data loss (daemon thread,
                            # no HTTP surface, so a WARNING is the only signal).
                            if isinstance(_lr, dict) and _lr.get("ok") is False \
                                    and _lr.get("skipped") is None:
                                _af_log.warning("chat_learner did NOT persist "
                                                "(repo=%s): %s", _repo, _lr.get("error"))
                            if isinstance(_pc, dict) and _pc.get("ok") is False \
                                    and _pc.get("skipped") is None:
                                _af_log.warning("preference_capture did NOT persist "
                                                "(repo=%s): %s", _repo, _pc.get("error"))
                            # USER COMMENT / TOPIC SUGGESTION the user explicitly
                            # states → md capture (repo + topic stamped) so it
                            # reaches the compaction axes. Preference turns are
                            # already captured above, so skip those.
                            try:
                                from aiforge_core.memory import md_store as _md2
                                from aiforge_core.runtime import capture_cues as _cc
                                _low = (prompt or "").lower()
                                _pref_done = isinstance(_pc, dict) and _pc.get("captured")
                                if any(s in _low for s in (
                                        "track ", "organize by", "organise by",
                                        "as a topic", "remember this topic", "topic:")):
                                    _md2.capture("topic_suggestion", (prompt or "").strip(),
                                                 repo=_repo, source=f"chat:{session_id or ''}")
                                elif not _pref_done and _cc.has_cue(prompt or ""):
                                    _md2.capture("user_comment", (prompt or "").strip(),
                                                 repo=_repo, source=f"chat:{session_id or ''}")
                            except Exception:  # noqa: BLE001
                                pass
                        except Exception as _lexc:  # noqa: BLE001
                            _af_log.warning("chat learn/capture thread failed: %s",
                                            _lexc)
                    from aiforge_core.runtime import background as _bg
                    _bg.spawn(_chat_learn, name="chat-learn")
                    # Boundary-gated per-SESSION summary → browsable md file +
                    # memory graph (Neo4j when configured). Refreshes an
                    # upsert'd summary every N turns as the session grows (one
                    # cheap-tier LLM call, capped) so cross-session recall goes
                    # through unified_query's graph instead of a substring scan.
                    # Best-effort on a daemon thread — a failure here must never
                    # affect the turn.
                    def _chat_summarize():
                        try:
                            from aiforge_core.runtime import chat_store, chat_summary
                            from aiforge_core.runtime.chat_agent import _chat_repo_key
                            every = 4
                            try:
                                every = max(1, int(os.environ.get(
                                    "AIFORGE_CHAT_SUMMARY_EVERY", "4")))
                            except (TypeError, ValueError):
                                every = 4
                            n = len(chat_store.get_messages(session_id))
                            if n <= 0 or n % every != 0:
                                return
                            _repo = _chat_repo_key(cwd)   # git-toplevel, matches recall
                            chat_summary.summarize_session(session_id, _repo)
                            # Auto-author a workflow from the session's WORKING
                            # steps + file it into OKR memory with tags, so the
                            # working commands are reusable and don't get redone.
                            from aiforge_core.runtime import session_ledger
                            session_ledger.capture_working_workflow(session_id, _repo)
                            # OKR-DAG auto-authoring: extract durable Objectives/
                            # KeyResults/Learnings from this session into the graph,
                            # and write a session node from the executed steps.
                            try:
                                from aiforge_core.memory import okf as _okr
                                from aiforge_core.runtime.chat_agent import _chat_repo_key
                                # An unpinned chat runs in an isolated scratch
                                # workspace (chat-workspaces/session-<id>) — NOT
                                # a real repo. Scope its knowledge GLOBAL instead
                                # of minting a phantom projects/session-<id>/ OKR
                                # tree (one bogus "project" per session).
                                _rkey = None if _is_isolated_workspace(cwd) \
                                    else _chat_repo_key(cwd)
                                _msgs2 = chat_store.get_messages(session_id) or []
                                _tx = "\n".join(
                                    f"{m.get('role')}: {m.get('content')}"
                                    for m in _msgs2 if isinstance(m, dict)
                                    and m.get("content"))[:8000]
                                # classify each learning global vs THIS repo
                                _okr.extract_and_save(_tx, repo=_rkey)
                                _led = session_ledger.ledger_block(session_id)
                                if _led:
                                    _okr.write_session_node(
                                        title=f"chat session {session_id}",
                                        body=_led, repo=_rkey)
                            except Exception:  # noqa: BLE001
                                pass
                        except Exception:  # noqa: BLE001
                            pass
                    _bg.spawn(_chat_summarize, name="chat-summarize")
            # Wake every subscriber (this stream + any /attach) and close THIS
            # run object (not by session id — a newer turn for the same session
            # may have already replaced it in the registry). Done LAST so a
            # re-attach during persistence still tails live.
            run.finish()
            try:
                _PRODUCE_SEM.release()
            except (ValueError, RuntimeError):   # never over-release
                pass

    _spawn(_produce, name="chat-produce")

    def _stream():
        # Tail the live run as SSE. A client disconnect only closes this
        # subscriber — the producer thread keeps running.
        q = run.subscribe()
        for ev in chat_runs.iter_subscription(run, q):
            yield f"data: {json.dumps(ev)}\n\n"

    return sse_response(_stream(), label=f"chat-session-{session_id}")


@router.get("/api/chat/sessions/{session_id}/attach")
def chat_session_attach(session_id: int) -> StreamingResponse:
    """Re-attach to an in-flight run after navigating back to the Chat view.

    Replays the run's buffered events (so the client rebuilds the live turn
    from the start — thoughts, tools, subtasks, the in-progress answer) and
    then tails live events to completion. If no run is in flight for this
    session, emits a single ``done`` immediately so the client knows there's
    nothing live to resume (and can just show the persisted history)."""
    from aiforge_core.runtime import chat_runs

    def _gen():
        # First event always tells the client whether there's a live run, so it
        # can decide to show progress (running) or just keep the persisted
        # history (not running) — no guessing from the event stream.
        run = chat_runs.get(session_id)
        running = bool(run and not run.done)
        _att = {"type": "attached", "running": running}
        if running and run is not None:
            _att["started_at"] = run.started_at   # epoch secs → true elapsed
        yield f"data: {json.dumps(_att)}\n\n"
        if not running or run is None:
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        q = run.subscribe()
        for ev in chat_runs.iter_subscription(run, q):
            yield f"data: {json.dumps(ev)}\n\n"

    return sse_response(_gen(), label=f"chat-attach-{session_id}")


@router.post("/api/chat/sessions/{session_id}/stop")
def chat_session_stop(session_id: int) -> dict:
    """Stop the in-flight chat run for this session — signals the agent
    loop / ADK pipeline to halt and kills any subprocess groups it
    spawned (builds, test runs). Idempotent."""
    from aiforge_core.runtime import chat_approve, chat_cancel
    active = chat_cancel.cancel(session_id)
    chat_approve.cancel(session_id)   # unblock any pending approval gate
    return {"stopped": active, "session_id": session_id}


@router.post("/api/chat/kill-all")
def chat_kill_all() -> dict:
    """Force-reset ALL in-flight chat state — the 'kill all' escape hatch.

    Recovers from a wedged run that left a session looking busy or made a new
    chat sit on 'waiting for another team run to finish' (the team run lock was
    held by a run that won't release it). Cancels every tracked run, clears the
    approval + steer gates, finishes every live-run buffer, and force-releases
    the team run-serialization lock. Idempotent and safe to hit any time."""
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        chat_pipeline,
        chat_runs,
    )
    sessions = chat_cancel.cancel_all()
    for sid in sessions:
        chat_approve.cancel(sid)
        chat_approve.finish(sid)
        chat_interject.clear(sid)
        # NOTE: do NOT chat_cancel.finish(sid) here — that pops the cancel token
        # microseconds after cancel_all() set it, before the (slow, between-poll)
        # producer can observe it, so the run kept executing. Leave the token
        # SET; each run's own finally pops it once it has actually torn down.
    chat_runs.finish_all()
    lock_freed = chat_pipeline.force_release_run_lock()
    return {"killed": sessions, "count": len(sessions),
            "team_lock_released": lock_freed}


class _SteerBody(BaseModel):
    content: str = Field(..., description="mid-run guidance to fold in")


@router.post("/api/chat/sessions/{session_id}/steer")
def chat_session_steer(session_id: int, body: _SteerBody) -> dict:
    """Inject a steer message into the IN-FLIGHT run for this session WITHOUT
    stopping it (Gap A — mid-run steering). The message is queued and folded
    into the agent's working context at its next safe step, so the agent
    adjusts course mid-run. No-op (queued:false) for blank content.

    Drained by: simple/plan's ReAct loop, the parallel-team subtask loop
    (folds into SPEC.md), and the sequential team ADK driver's Doer/Refiner
    before_model callback (chat_steer_callback). Only best-of-N never
    drains, so steering there would queue a message no loop ever reads —
    detect that and report it unsupported rather than falsely claiming the
    steer was queued."""
    from aiforge_core.runtime import chat_interject
    # Atomic test-and-set: push() itself checks steerability under its lock, so
    # there's no window between the check and the enqueue for a run-end clear()
    # to slip a stale steer into the next turn (CC3).
    queued = chat_interject.push(session_id, body.content, require_steerable=True)
    if queued:
        return {"queued": True, "session_id": session_id}
    # Refused — distinguish blank content from a non-steerable (best-of-N) run.
    if not (body.content or "").strip():
        return {"queued": False, "session_id": session_id, "reason": "empty content"}
    return {"queued": False, "unsupported": True, "session_id": session_id,
            "reason": "steering not available for this run"}


class _ApproveBody(BaseModel):
    decision: str = Field(..., description="'approve' | 'reject'")
    id: int | None = Field(None, description="approval seq id echoed from the event")
    note: str | None = None


@router.post("/api/chat/sessions/{session_id}/approve")
def chat_session_approve(session_id: int, body: _ApproveBody) -> dict:
    """Resolve a pending approval gate (#1) — the chat run is blocked
    waiting for the user's Approve/Reject on a risky/ask-policy action."""
    from aiforge_core.runtime import chat_approve
    ok = chat_approve.resolve(session_id, body.decision, body.note or "", body.id)
    return {"resolved": ok, "decision": body.decision, "session_id": session_id}


class _CheckpointBody(BaseModel):
    label: str | None = Field(None, description="human label for the snapshot")


@router.get("/api/chat/sessions/{session_id}/checkpoints")
def chat_session_checkpoints(session_id: int) -> dict:
    """List workspace checkpoints (#3) for this session's working dir."""
    from aiforge_core.runtime import chat_store, checkpoints
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    return {"checkpoints": checkpoints.list_checkpoints(cwd)}


@router.post("/api/chat/sessions/{session_id}/checkpoints", status_code=201)
def chat_session_checkpoint_create(session_id: int, body: _CheckpointBody) -> dict:
    """Snapshot the session's working dir (#3) to a hidden git ref."""
    import datetime as _dt

    from aiforge_core.runtime import chat_store, checkpoints
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return checkpoints.snapshot(cwd, label=body.label or "manual", when=when)


class _RestoreBody(BaseModel):
    sha: str = Field(..., min_length=4)
    paths: list[str] | None = Field(
        None, description="restore ONLY these paths (files-only / subset restore); "
                          "omit to restore the whole snapshot")
    delete_orphans: bool = Field(
        False, description="full-state restore: also delete files created after "
                           "the checkpoint so the tree exactly matches it")


@router.post("/api/chat/sessions/{session_id}/checkpoints/restore")
def chat_session_checkpoint_restore(session_id: int, body: _RestoreBody) -> dict:
    """Restore the session's working dir to a checkpoint (#3).

    Granularity: ``paths`` restores a subset; ``delete_orphans`` makes it a
    full-state restore (matching the snapshot exactly)."""
    from aiforge_core.runtime import chat_store, checkpoints
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    return checkpoints.restore(cwd, body.sha, paths=body.paths or None,
                               delete_orphans=bool(body.delete_orphans))


class _SessionTicketBody(BaseModel):
    content: str = Field(..., min_length=1)
    project: str | None = Field(None, description="target repo; defaults to session cwd name")


@router.post("/api/chat/sessions/{session_id}/ticket", status_code=201)
def chat_session_ticket(session_id: int, body: _SessionTicketBody) -> dict:
    """Pipeline mode: turn a chat message into a real ticket that runs the
    full architect→planner→verifier→doer→feedback→learner pipeline. The
    runner picks it up (urgent priority → next); the chat UI streams live
    stage updates from ``/api/trace/{identifier}/stream``. Returns the
    created ticket identifier + trace stream path."""
    from aiforge_core.runtime import chat_store
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    project = (body.project or "").strip() or os.path.basename(
        os.path.normpath(session.get("cwd") or _default_cwd())) or None
    title = body.content.strip().splitlines()[0][:120] or "chat request"
    if (session.get("title") or "New chat") == "New chat":
        chat_store.rename_session(session_id, title)
    t = tickets_mod.create(
        title=title, body=body.content.strip(), project=project,
        priority="urgent", route="code",
        # interactive=chat → the runner's clarify step may ask questions
        # before running. Normal tickets omit this → static, no ask.
        metadata={"source": "chat", "chat_session_id": session_id,
                  "interactive": True},
    )
    chat_store.add_message(session_id, "user", body.content)
    chat_store.add_message(
        session_id, "assistant",
        f"Started pipeline run as **{t.identifier}** (project `{project or '—'}`). "
        f"Streaming stage updates…",
        [{"type": "ticket", "identifier": t.identifier, "project": project}],
    )
    return {"ticket": t.identifier, "ticket_id": t.id, "project": project,
            "trace_url": f"/api/tickets/{t.identifier}/events/stream"}
