"""Shared router, the producer-slot cap, approval settings, and the one-shot
ask / retain / agent endpoints."""
from __future__ import annotations

import json
import logging
import os
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aiforge_core.api.routes._sse import sse_response

_NEW_CHAT = 'New chat'

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


@router.put("/api/chat/approval-settings/{mode}", responses={400: {"description": "Bad request"}})
def approval_settings_set(mode: str, body: _ApprovalModeBody) -> dict:
    """Enable/disable approvals for one mode. `mode` is chat | plan | pipeline."""
    from aiforge_core.config import approval_settings
    try:
        approval_settings.set_mode(mode, body.enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return approval_settings_get()


class _ResponseLanguageBody(BaseModel):
    language: str | None = Field(None, description='"en-IN", "en-US" or "" (no preference); omit to keep')
    style: str | None = Field(None, description='"simple", "formal" or "" (no preference); omit to keep')


@router.get("/api/chat/response-language")
def response_language_get() -> dict:
    """The English variety and tone every model writes in, and the choices."""
    from aiforge_core.config import response_language as rl
    return {"language": rl.get(), "style": rl.get_style(),
            "options": rl.options(), "styles": rl.style_options()}


@router.put("/api/chat/response-language", responses={400: {"description": "Bad request"}})
def response_language_set(body: _ResponseLanguageBody) -> dict:
    """Choose the language and/or tone. Unknown values are refused."""
    from aiforge_core.config import response_language as rl
    try:
        if body.language is not None:
            rl.set_language(body.language)
        if body.style is not None:
            rl.set_style(body.style)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return response_language_get()


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


def _request_repo_root() -> "str | None":
    from aiforge_core.runtime import request_context
    return request_context.get_repo_root()


def _default_cwd() -> str:
    return (
        os.environ.get("AIFORGE_WORKSPACE_DIR")
        or _request_repo_root()
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
