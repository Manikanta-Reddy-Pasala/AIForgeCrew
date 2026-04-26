"""Local mlx-lm provider — Mac Studio mlx_lm.server on the role port.

Per-role env override:
- ``AIFORGE_<ROLE>_BASE_URL`` (e.g. AIFORGE_PLANNER_BASE_URL)
- ``AIFORGE_<ROLE>_MODEL``
- ``AIFORGE_<ROLE>_API_KEY`` (rarely needed for mlx-lm; "sk-local")

Falls back to ``AIFORGE_LM_BASE_URL`` / ``LM_STUDIO_BASE_URL`` for
the URL when no per-role var set.
"""
from __future__ import annotations

import os

from ..types import Endpoint
from . import register_provider


class LocalMlxProvider:
    name = "local"

    def is_available(self) -> bool:
        # Treat as always-available; runtime errors propagate to caller.
        return True

    def rate_limits(self) -> dict | None:
        # Local model — no rate limit. Caller short-circuits acquire().
        return None

    def endpoint(self, role: str) -> Endpoint:
        role_up = role.upper()
        base_url = (
            os.environ.get(f"AIFORGE_{role_up}_BASE_URL")
            or os.environ.get("AIFORGE_LM_BASE_URL")
            or "http://127.0.0.1:1234/v1"
        )
        if not base_url.endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"
        api_key = (
            os.environ.get(f"AIFORGE_{role_up}_API_KEY")
            or os.environ.get("LM_STUDIO_API_KEY")
            or "sk-local"
        )
        model = (
            os.environ.get(f"AIFORGE_{role_up}_MODEL")
            or "mlx-local"
        )
        # mlx-lm honours chat_template_kwargs for thinking-mode flips.
        extras: dict = {}
        think_env = os.environ.get(f"AIFORGE_{role_up}_THINK")
        if think_env == "1":
            extras["chat_template_kwargs"] = {"enable_thinking": True}
        elif think_env == "0":
            extras["chat_template_kwargs"] = {"enable_thinking": False}
        return Endpoint(
            base_url=base_url, api_key=api_key, model=model,
            provider=self.name, role=role, extras=extras,
        )


register_provider(LocalMlxProvider())
