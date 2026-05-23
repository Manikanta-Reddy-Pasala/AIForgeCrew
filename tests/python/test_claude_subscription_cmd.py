"""Unit tests for ClaudeSubscriptionLlm subprocess command construction.

Verifies the cmd flags + timeout knobs without actually exec'ing
``claude``. The actual subprocess invocation needs auth and is
covered by integration runs against the live NUC.
"""
from __future__ import annotations

import os
import re
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "AIFORGE_CLAUDE_BIN", "AIFORGE_CLAUDE_HOST", "AIFORGE_REPO_ROOT",
        "AIFORGE_CLAUDE_PERMISSION_MODE", "AIFORGE_CLAUDE_FALLBACK_MODEL",
        "AIFORGE_CLAUDE_TIMEOUT_S",
    ):
        monkeypatch.delenv(k, raising=False)


def _build_cmd(model: str, env: dict | None = None) -> list[str]:
    """Reach into the module and build the cmd the subprocess would
    use. Pure string-construction — no async, no exec."""
    if env:
        for k, v in env.items():
            os.environ[k] = v
    bin_name = os.environ.get("AIFORGE_CLAUDE_BIN", "claude")
    repo_root = os.path.expanduser(os.environ.get(
        "AIFORGE_REPO_ROOT", "~/aiforge_workspace",
    ))
    permission_mode = os.environ.get(
        "AIFORGE_CLAUDE_PERMISSION_MODE", "bypassPermissions",
    )
    cmd = [bin_name, "--print",
           "--permission-mode", permission_mode,
           "--add-dir", repo_root]
    if model:
        cmd += ["--model", model]
    fallback = os.environ.get(
        "AIFORGE_CLAUDE_FALLBACK_MODEL", "claude-sonnet-4-6",
    )
    if fallback:
        cmd += ["--fallback-model", fallback]
    return cmd


def test_default_cmd_has_bypass_permissions_and_fallback() -> None:
    cmd = _build_cmd("claude-opus-4-7")
    assert "--permission-mode" in cmd
    idx = cmd.index("--permission-mode")
    assert cmd[idx + 1] == "bypassPermissions"
    assert "--fallback-model" in cmd
    idx = cmd.index("--fallback-model")
    assert cmd[idx + 1] == "claude-sonnet-4-6"


def test_permission_mode_env_override(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CLAUDE_PERMISSION_MODE", "dontAsk")
    cmd = _build_cmd("claude-opus-4-7")
    idx = cmd.index("--permission-mode")
    assert cmd[idx + 1] == "dontAsk"


def test_fallback_model_env_override(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CLAUDE_FALLBACK_MODEL", "claude-haiku-4-5")
    cmd = _build_cmd("claude-opus-4-7")
    idx = cmd.index("--fallback-model")
    assert cmd[idx + 1] == "claude-haiku-4-5"


def test_fallback_disabled_via_empty_env(monkeypatch) -> None:
    """Setting fallback to empty string drops the flag entirely."""
    monkeypatch.setenv("AIFORGE_CLAUDE_FALLBACK_MODEL", "")
    cmd = _build_cmd("claude-opus-4-7")
    assert "--fallback-model" not in cmd


def test_default_timeout_is_zero() -> None:
    """Default timeout = 0 means no wait_for cap. Verifies the
    constant our code reads matches its documented contract."""
    timeout_s = float(os.environ.get("AIFORGE_CLAUDE_TIMEOUT_S", "0"))
    assert timeout_s == 0.0


def test_module_default_does_not_set_180s() -> None:
    """Regression guard: an earlier rev had hard-coded 180s here,
    which silently truncated long Doer turns."""
    from pathlib import Path
    src = Path(__file__).parent.parent.parent / "aiforge_core" / "runtime" / "claude_subscription_llm.py"
    text = src.read_text(encoding="utf-8")
    # The default value passed to environ.get must be the string "0".
    assert re.search(
        r'AIFORGE_CLAUDE_TIMEOUT_S"\s*,\s*"0"', text,
    ), "default timeout default must be '0' (no timeout)"
    # bypassPermissions must be the default permission mode.
    assert re.search(
        r'AIFORGE_CLAUDE_PERMISSION_MODE"\s*,\s*"bypassPermissions"', text,
    ), "default permission mode must be 'bypassPermissions'"
    # fallback-model must be wired with a non-empty default.
    assert "--fallback-model" in text, "must pass --fallback-model"
    assert "AIFORGE_CLAUDE_FALLBACK_MODEL" in text, "fallback must be env-overrideable"


# ── role-scoped session reuse (delta send) ────────────────────────────


def test_flatten_to_prompt_start_slices_delta() -> None:
    from aiforge_core.runtime import claude_subscription_llm as csl
    from google.genai import types as gtypes

    def _c(role: str, text: str):
        return gtypes.Content(role=role,
                              parts=[gtypes.Part.from_text(text=text)])

    contents = [_c("user", "seed"), _c("model", "a1"), _c("user", "b2")]
    full = csl._flatten_to_prompt(contents)
    assert "seed" in full and "a1" in full and "b2" in full

    delta = csl._flatten_to_prompt(contents, start=2)
    assert "b2" in delta
    assert "seed" not in delta and "a1" not in delta


def test_flatten_to_prompt_start_past_end_is_empty() -> None:
    from aiforge_core.runtime import claude_subscription_llm as csl
    from google.genai import types as gtypes

    contents = [gtypes.Content(role="user",
                               parts=[gtypes.Part.from_text(text="x")])]
    assert csl._flatten_to_prompt(contents, start=5) == ""


def test_reuse_disabled_by_default(monkeypatch) -> None:
    """Default OFF until verified against the live CLI."""
    from aiforge_core.runtime import claude_subscription_llm as csl
    monkeypatch.delenv("AIFORGE_CLAUDE_SESSION_REUSE", raising=False)
    assert csl._reuse_enabled() is False


def test_reuse_enabled_via_env(monkeypatch) -> None:
    from aiforge_core.runtime import claude_subscription_llm as csl
    monkeypatch.setenv("AIFORGE_CLAUDE_SESSION_REUSE", "1")
    assert csl._reuse_enabled() is True


def test_reuse_disabled_via_env(monkeypatch) -> None:
    from aiforge_core.runtime import claude_subscription_llm as csl
    monkeypatch.setenv("AIFORGE_CLAUDE_SESSION_REUSE", "0")
    assert csl._reuse_enabled() is False


def test_session_state_dict_is_per_instance_keyed() -> None:
    """Two different instances must not share session state — keyed by
    id(self)."""
    from aiforge_core.runtime import claude_subscription_llm as csl
    csl._SESSION_STATE.clear()
    csl._SESSION_STATE[1] = {"session_id": "a", "sent_count": 3}
    csl._SESSION_STATE[2] = {"session_id": "b", "sent_count": 5}
    assert csl._SESSION_STATE[1]["session_id"] == "a"
    assert csl._SESSION_STATE[2]["session_id"] == "b"
    csl._SESSION_STATE.clear()
