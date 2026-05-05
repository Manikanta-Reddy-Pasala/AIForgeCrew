"""Claude (subscription) provider — shells out to `claude` CLI via subprocess.

This provider lets AIForge use a **Claude Pro/Team subscription** (OAuth-via-keychain)
as a backend, NOT the Anthropic API. Useful when the operator already pays for
Claude subscription and wants to avoid double-billing for API access.

## How it works

The orchestrator's `aiforge_core.llm.client.complete()` calls this provider's
`endpoint(role)`, gets an `Endpoint(provider="claude_local", base_url="claude:cli", ...)`,
then `client.py`'s send-loop branches on `ep.provider == "claude_local"` to invoke
`subprocess.run(["claude", "--print", ...])` instead of HTTP POST.

## Per-role env overrides

- ``AIFORGE_<ROLE>_CLAUDE_MODEL`` — picks the Claude model id (default `claude-opus-4-7`)
- ``AIFORGE_CLAUDE_BIN`` — path to the `claude` binary (default `claude` from PATH)
- ``AIFORGE_CLAUDE_HOST`` — when set, run `ssh <host> claude --print …` instead of local;
  enables NUC→Mac-Studio routing where the subscription keychain lives on the Mac

## Setup notes

1. Install `claude` CLI on the host that holds the subscription keychain.
2. Run `claude --auth` once interactively to seed the OAuth token.
3. Verify: `echo "say hi" | claude --print` returns text.
4. Set `AIFORGE_PRIMARY_BACKEND=claude_local` (or per-role
   `AIFORGE_<ROLE>_PROVIDER=claude_local`).

## Known limitations

- Claude CLI is **rate-limited per subscription tier**; no programmatic quotas.
- No streaming via `--print` — full response only.
- No native tool-call schema; system prompt steers behaviour.
- Subprocess startup adds ~500-1500ms per call.
"""
from __future__ import annotations

import os
import shutil

from ..types import Endpoint
from . import register_provider


_DEFAULT_MODEL = "claude-opus-4-7"


class ClaudeLocalProvider:
    """Claude subscription via `claude` CLI subprocess.

    `endpoint()` returns a marker Endpoint with base_url ``claude:cli``.
    The actual subprocess call lives in
    ``aiforge_core.llm.client._send_via_claude_cli`` — gated on
    ``ep.provider == "claude_local"``.
    """

    name = "claude_local"
    hidden = False

    def is_available(self) -> bool:
        bin_name = os.environ.get("AIFORGE_CLAUDE_BIN", "claude")
        # Local install OR remote via ssh — the latter only checks env presence.
        if os.environ.get("AIFORGE_CLAUDE_HOST"):
            return True
        return shutil.which(bin_name) is not None

    def rate_limits(self) -> dict | None:
        # Subscription rate limits are tier-specific and not exposed
        # programmatically — leave unset.
        return None

    def endpoint(self, role: str) -> Endpoint:
        role_up = role.upper()
        model = (
            os.environ.get(f"AIFORGE_{role_up}_CLAUDE_MODEL")
            or os.environ.get("AIFORGE_CLAUDE_MODEL")
            or _DEFAULT_MODEL
        )
        return Endpoint(
            base_url="claude:cli",
            api_key="",  # subscription auth via keychain, no API key
            model=model,
            provider=self.name,
            role=role,
            extras={
                "claude_bin": os.environ.get("AIFORGE_CLAUDE_BIN", "claude"),
                "claude_host": os.environ.get("AIFORGE_CLAUDE_HOST", ""),
            },
        )


register_provider(ClaudeLocalProvider())
