"""Settings for chat failover (config/chat_failover): the 1-3 models a chat
turn switches to when its model stops answering, and the two thresholds."""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ._core import (
    router,
)


class _FailoverBody(BaseModel):
    models: list[str] = Field(default_factory=list,
                              description="registry ids, in failover order")
    stall_s: int | None = Field(None, description="None = env / default")
    fails: int | None = Field(None, description="None = env / default")


def _with_options(view: dict) -> dict:
    """The view plus the registry models the owner can pick from."""
    from aiforge_core.config import model_registry
    try:
        opts = [{"id": m.get("id"), "label": m.get("label"), "model": m.get("model"),
                 "base_url": m.get("base_url") or ""}
                for m in model_registry.list_models()]
    except Exception:  # noqa: BLE001 — an unreadable registry offers nothing
        opts = []
    return {**view, "options": opts}


@router.get("/api/chat/failover")
def chat_failover_get() -> dict:
    """The failover list, its effective thresholds, and the models to pick."""
    from aiforge_core.config import chat_failover
    return _with_options(chat_failover.view())


@router.put("/api/chat/failover", responses={400: {"description": "Bad request"}})
def chat_failover_set(body: _FailoverBody) -> dict:
    """Replace the failover list (and thresholds)."""
    from aiforge_core.config import chat_failover
    try:
        view = chat_failover.save(body.models, body.stall_s, body.fails)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(409, str(exc))
    return _with_options(view)
