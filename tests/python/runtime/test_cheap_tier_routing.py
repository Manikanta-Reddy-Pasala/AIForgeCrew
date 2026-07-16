"""Changes 2 + 3 — cheap-tier routing for throwaway ops.

Change 2: chat-title generation (a ~20-token throwaway) routes to the cheap
``triage`` role instead of the big chat/doer role.
Change 3: cheap roles (triage, enhancer) fall back to ``AIFORGE_CHEAP_MODEL``
when no explicit per-role pin is set — before the global default.
"""
from __future__ import annotations

import pathlib

from aiforge_core.config import agent_config as ac
from aiforge_core.runtime import chat_title


# ─── Change 2: title uses the cheap triage role ───────────────────────


def test_suggest_title_forwards_role_to_client(monkeypatch):
    seen = {}

    def fake_complete(role, convo, **kw):
        seen["role"] = role
        return "Fix The Login Bug"

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)
    out = chat_title.suggest_title("fix the login bug", role="triage")
    assert out == "Fix The Login Bug"
    assert seen["role"] == "triage"


def test_api_titles_on_triage_role():
    """The chat session-message call site pins the cheap role for titling.
    (Moved from api.py into api/routes/chat.py during the APIRouter split.)"""
    # ac is now a package (config/agent_config/__init__.py), so aiforge_core
    # is parents[2] (was parents[1] when agent_config was a plain module).
    src = pathlib.Path(ac.__file__).parents[2] / "api" / "routes" / "chat.py"
    text = src.read_text(encoding="utf-8")
    assert 'suggest_title(prompt, role="triage")' in text


# ─── Change 3: AIFORGE_CHEAP_MODEL fallback for cheap roles ───────────


def test_cheap_model_for_returns_env_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHEAP_MODEL", "tiny-1b")
    monkeypatch.delenv("AIFORGE_TRIAGE_MODEL", raising=False)
    assert ac.cheap_model_for("triage") == "tiny-1b"
    assert ac.cheap_model_for("enhancer") == "tiny-1b"


def test_cheap_model_for_none_when_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CHEAP_MODEL", raising=False)
    assert ac.cheap_model_for("triage") is None


def test_cheap_model_for_none_for_non_cheap_role(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHEAP_MODEL", "tiny-1b")
    assert ac.cheap_model_for("doer") is None
    assert ac.cheap_model_for("planner") is None


def test_cheap_model_for_yields_to_env_pin(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHEAP_MODEL", "tiny-1b")
    monkeypatch.setenv("AIFORGE_TRIAGE_MODEL", "pinned-big")
    assert ac.cheap_model_for("triage") is None


def test_resolve_litellm_uses_cheap_model(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHEAP_MODEL", "tiny-1b")
    monkeypatch.delenv("AIFORGE_TRIAGE_MODEL", raising=False)
    got = ac.resolve_litellm("triage")
    assert got["model_id"].endswith("tiny-1b")
