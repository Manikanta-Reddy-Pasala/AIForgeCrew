"""Hermes agent driver — load prompts, run tool-call loop, enforce checkpoints.

Usage:
    a = Agent.load(repo_root, role="sr-developer", model="qwen3.6-35b-a3b")
    reply = a.run(ticket_id, user_message, store=paperclip_store)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from paperclip.budget import BudgetExceeded, Spend, assert_within_budget, record
from paperclip.config import PaperclipConfig
from paperclip.permissions import PermissionDenied
from paperclip.store import Store

from .llm import LLMClient, LLMReply
from .tools import ToolRegistry, build_default_registry


# Mapping of DESIGN role → LM Studio model id seen on `/v1/models`.
ROLE_MODEL_DEFAULT = {
    "em":            None,                       # cloud, caller must set
    "tester":        "zai-org/glm-4.7-flash",
    "sr-developer":  "qwen3.6-35b-a3b",
    "sr-architect":  "gemma-4-31b-it",
}


@dataclass
class Agent:
    role: str
    model: str | None
    system_prompt: str
    contract: str
    registry: ToolRegistry
    client: LLMClient
    repo_root: Path
    max_tool_calls: int = 15            # DESIGN §9.3 checkpoint interval
    stop_after_checkpoints: int = 3     # hard cap on tool-call rounds

    @classmethod
    def load(
        cls,
        repo_root: Path,
        role: str,
        *,
        model: str | None = None,
        client: LLMClient | None = None,
    ) -> "Agent":
        sp = (repo_root / "agents" / role / "system-prompt.md").read_text()
        ct = (repo_root / "agents" / role / "contract.md").read_text()
        registry = build_default_registry(repo_root, role)
        if client is None:
            client = LLMClient.cloud() if role == "em" else LLMClient.local()
        if model is None:
            model = ROLE_MODEL_DEFAULT.get(role)
        return cls(
            role=role,
            model=model,
            system_prompt=sp,
            contract=ct,
            registry=registry,
            client=client,
            repo_root=repo_root,
        )

    def _build_messages(self, user_message: str, ticket_id: str | None) -> list[dict]:
        sys = f"""{self.system_prompt}

--- CONTRACT ---
{self.contract}

--- RUNTIME CONTEXT ---
You are the {self.role} agent. Current ticket: {ticket_id or '(none)'}.
You may only use the tools declared. All file paths are repo-relative.
Stop after completing your contract's OUT deliverable."""
        return [
            {"role": "system", "content": sys},
            {"role": "user", "content": user_message},
        ]

    def run(
        self,
        ticket_id: str | None,
        user_message: str,
        *,
        store: Store | None = None,
        cfg: PaperclipConfig | None = None,
    ) -> LLMReply:
        """Run the tool-call loop. Enforces budget via cfg + store (if both given)."""
        if self.model is None:
            raise RuntimeError(f"no model configured for role {self.role}")

        messages = self._build_messages(user_message, ticket_id)
        tools = self.registry.openai_schema(self.role)

        rounds = 0
        last: LLMReply | None = None

        while True:
            rounds += 1
            reply = self.client.chat(
                model=self.model,
                messages=messages,
                tools=tools if tools else None,
                max_tokens=3000,
                temperature=0.0,
            )
            last = reply

            # Budget bookkeeping — each round records completion tokens as "spend".
            if store is not None and cfg is not None and ticket_id is not None:
                spend = Spend(
                    role=self.role,
                    tokens=reply.usage["completion_tokens"] + reply.usage["prompt_tokens"],
                )
                assert_within_budget(cfg, store, ticket_id, self.role, spend)
                record(store, ticket_id, spend)

            if not reply.tool_calls:
                break

            # Append the assistant turn with its tool-call payload, then append
            # each tool result message. OpenAI-compat schema.
            messages.append({
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": reply.tool_calls,
            })
            for tc in reply.tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = self.registry.dispatch(self.role, name, args)
                    payload = {"ok": True, "result": result}
                except PermissionDenied as e:
                    payload = {"ok": False, "error": f"permission_denied: {e}"}
                except Exception as e:
                    payload = {"ok": False, "error": f"{type(e).__name__}: {e}"}

                if store is not None and ticket_id is not None:
                    store.audit_event(ticket_id, "tool_call", self.role,
                                      {"tool": name, "args": args, "ok": payload.get("ok", False)})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": name,
                    "content": json.dumps(payload, default=str)[:8000],
                })

            # Circuit-breaker per §9.3 — checkpoint every N tool calls.
            if rounds >= self.stop_after_checkpoints:
                break

        return last  # type: ignore[return-value]
