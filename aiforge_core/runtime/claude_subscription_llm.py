"""ADK ``BaseLlm`` wrapper around the ``claude`` CLI subscription.

Runs Anthropic's ``claude --print`` as a subprocess instead of hitting the
API. Lets the v6 ADK SequentialAgent participate in the operator's Claude
Pro/Team subscription quota — no per-token billing, OAuth keychain auth.

Wired into ``runtime.adk_runner._build_litellm_model`` when
``agent_config.resolve_litellm`` returns ``_claude_cli=True``.

Limitations carried over from the underlying CLI:
- No streaming through ``--print`` (full response, single chunk).
- No native tool-call schema; system prompt steers tool use.
- Subprocess startup ~500-1500ms per call.
- Rate-limited per subscription tier (Pro/Team), not exposed
  programmatically.

Honours the same env knobs as ``aiforge_core.llm.providers.claude_local``:
``AIFORGE_CLAUDE_BIN`` (default ``claude``) and ``AIFORGE_CLAUDE_HOST``
(SSH-route NUC → Mac Studio when keychain is on a different host).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types as gtypes

log = logging.getLogger("claude_subscription_llm")


def _flatten_to_prompt(contents: list[gtypes.Content] | None) -> str:
    """Concatenate ADK Content parts into a single prompt the CLI takes
    on stdin. Loses fine-grained role tagging (CLI is single-turn) but
    preserves order. Each turn is prefixed with a role marker so the
    model can still distinguish system vs user vs assistant context."""
    if not contents:
        return ""
    parts: list[str] = []
    for c in contents:
        role = (getattr(c, "role", None) or "user").strip() or "user"
        text_chunks: list[str] = []
        for p in (c.parts or []):
            t = getattr(p, "text", None)
            if t:
                text_chunks.append(t)
        if text_chunks:
            parts.append(f"<|{role}|>\n" + "\n".join(text_chunks))
    return "\n\n".join(parts)


class ClaudeSubscriptionLlm(BaseLlm):
    """``claude --print`` subprocess as an ADK BaseLlm.

    Construct with ``model="claude-opus-4-7"`` (or any id the local
    ``claude`` CLI accepts via ``--model``). The ``model`` field on
    BaseLlm is the only positional contract — everything else flows
    from env at call time.
    """

    @classmethod
    def supported_models(cls) -> list[str]:
        # Match any claude-* id; the CLI validates the actual model.
        return [r"claude-.*"]

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """One-shot subprocess call. Streaming flag is ignored (CLI
        ``--print`` returns the full response only)."""
        prompt = _flatten_to_prompt(llm_request.contents)
        # Pull system instruction out of config when the agent supplied one.
        sys_text = ""
        cfg = getattr(llm_request, "config", None)
        if cfg is not None:
            sys_inst = getattr(cfg, "system_instruction", None)
            if isinstance(sys_inst, str):
                sys_text = sys_inst
            elif sys_inst is not None:
                # Could be a Content object — flatten its parts.
                sys_parts = getattr(sys_inst, "parts", None) or []
                sys_text = "\n".join(
                    p.text for p in sys_parts if getattr(p, "text", None)
                )
        if sys_text:
            prompt = f"<|system|>\n{sys_text}\n\n{prompt}"

        bin_name = os.environ.get("AIFORGE_CLAUDE_BIN", "claude")
        host = os.environ.get("AIFORGE_CLAUDE_HOST", "")
        cmd = [bin_name, "--print"]
        if self.model:
            cmd += ["--model", self.model]
        if host:
            cmd = ["ssh", host, " ".join(cmd)]

        log.info("claude_subscription invoking: %s", cmd)
        timeout_s = float(os.environ.get("AIFORGE_CLAUDE_TIMEOUT_S", "180"))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            yield LlmResponse(
                content=gtypes.Content(
                    role="model",
                    parts=[gtypes.Part.from_text(
                        text=f"[claude_subscription error] timeout after {timeout_s}s",
                    )],
                ),
                error_code="TIMEOUT",
                error_message=f"claude CLI exceeded {timeout_s}s",
                turn_complete=True, finish_reason="STOP",
            )
            return

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", "replace")[:500]
            log.error("claude_subscription rc=%s stderr=%s",
                      proc.returncode, err)
            yield LlmResponse(
                content=gtypes.Content(
                    role="model",
                    parts=[gtypes.Part.from_text(
                        text=f"[claude_subscription error] rc={proc.returncode}: {err}",
                    )],
                ),
                error_code="SUBPROCESS_FAIL",
                error_message=err,
                turn_complete=True, finish_reason="STOP",
            )
            return

        text = (stdout or b"").decode("utf-8", "replace").strip()
        yield LlmResponse(
            content=gtypes.Content(
                role="model",
                parts=[gtypes.Part.from_text(text=text)],
            ),
            model_version=self.model,
            turn_complete=True, finish_reason="STOP",
        )
