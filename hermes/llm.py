"""LLM client — OpenAI-compat wrapper for both LM Studio (local) and cloud.

Stdlib-only (urllib). Returns (content|reasoning_content, usage{prompt_tokens,
completion_tokens, reasoning_tokens}). Supports tool calls via `tools=[...]`
in the request; assistant can return `tool_calls` in the first choice.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class LLMReply:
    content: str
    reasoning: str
    tool_calls: list[dict]
    usage: dict


@dataclass
class LLMClient:
    endpoint: str            # e.g. http://localhost:1234/v1
    api_key: str | None = None
    timeout_s: float = 300.0

    @classmethod
    def local(cls, endpoint: str | None = None) -> "LLMClient":
        return cls(endpoint=endpoint or os.environ.get("LLM_ENDPOINT", "http://localhost:1234/v1"))

    @classmethod
    def cloud(cls) -> "LLMClient":
        ep = os.environ.get("CLOUD_LLM_ENDPOINT")
        key = os.environ.get("CLOUD_LLM_API_KEY")
        if not ep:
            raise RuntimeError("CLOUD_LLM_ENDPOINT not set; EM role requires cloud LLM")
        return cls(endpoint=ep, api_key=key)

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_tokens: int = 3000,
        temperature: float = 0.0,
    ) -> LLMReply:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.endpoint.rstrip('/')}/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")

        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            resp = json.loads(r.read().decode())

        msg = (resp.get("choices") or [{}])[0].get("message", {})
        usage = resp.get("usage") or {}
        return LLMReply(
            content=(msg.get("content") or ""),
            reasoning=(msg.get("reasoning_content") or ""),
            tool_calls=(msg.get("tool_calls") or []),
            usage={
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
                "reasoning_tokens": int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)),
            },
        )

    def list_models(self) -> list[str]:
        req = urllib.request.Request(f"{self.endpoint.rstrip('/')}/models")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            data = json.loads(r.read().decode())
        return [m["id"] for m in (data.get("data") or [])]
