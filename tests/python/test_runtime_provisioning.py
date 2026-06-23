"""Team-mode self-provisioning + claude-absent fallback + delete guard."""
import importlib

import pytest


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    import os
    for k in list(os.environ):
        if k.startswith("AIFORGE_") and (
            k.endswith("_PROVIDER") or k.endswith("_BASE_URL")
            or k.endswith("_API_KEY") or k.endswith("_MODEL")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("AIFORGE_DEFAULT_PROVIDER", raising=False)
    import aiforge_core.config.agent_config as acfg
    importlib.reload(acfg)
    return acfg


# ── unknown roles (enhancer/validator) inherit the global default ────
def test_unknown_role_resolves_to_global_default(cfg):
    cfg.set_role("_default", "openai_compatible", "qwen3.6-35b",
                 base_url="https://chat.ai.internal/proxy/qwen36-35b/v1",
                 api_key="sk", insecure_tls=True)
    for role in ("enhancer", "validator", "some_future_role"):
        out = cfg.resolve_litellm(role)
        assert out["api_base"] == "https://chat.ai.internal/proxy/qwen36-35b/v1"
        assert out["insecure_tls"] is True


def test_unknown_role_defaults_local_without_global(cfg):
    # No global default set → unknown role resolves (via _row_for) to local.
    assert cfg.resolve_litellm("enhancer")["model_id"].startswith("openai/")
    assert cfg._row_for("enhancer")["provider"] == "local"
    # get() stays strict (observability depends on the raise)
    import pytest as _pt
    with _pt.raises(ValueError):
        cfg.get("enhancer")


# ── ensure_runtime ───────────────────────────────────────────────────
def test_ensure_runtime_reports_present_tool():
    from aiforge_core.runtime.tools.ensure_runtime import ensure_runtime
    out = ensure_runtime(["python3"])
    assert out["ok"] is True
    assert out["results"]["python3"]["present"] is True


def test_ensure_runtime_no_install_when_disabled(monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_INSTALL", "0")
    from aiforge_core.runtime.tools.ensure_runtime import ensure_runtime
    out = ensure_runtime(["definitely-not-a-real-tool-xyz"])
    assert out["ok"] is False
    assert "AIFORGE_ALLOW_INSTALL=0" in out["results"][
        "definitely-not-a-real-tool-xyz"]["error"]


def test_ensure_runtime_is_a_doer_tool():
    from aiforge_core.runtime.doer_tools import adk_function_tools
    names = {getattr(t, "name", getattr(getattr(t, "func", None), "__name__", ""))
             for t in adk_function_tools()}
    assert "ensure_runtime" in names


# ── delete guard ─────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd,blocked", [
    ("rm -rf build/", True),
    ("rm file.txt", True),
    ("git clean -fd", True),
    ("git reset --hard HEAD", True),
    ("drop table users", True),
    ("docker rm aiforge-api", True),
    ("kubectl delete pod x", True),
    ("npm install", False),
    ("mvn clean package", False),
    ("python3 main.py", False),
    ("git add -A && git commit -m x", False),
    ("mkdir build && cp a b", False),
])
def test_delete_guard(cmd, blocked):
    from aiforge_core.runtime.tools import delete_guard
    assert delete_guard.is_destructive_delete(cmd) is blocked


def test_chat_run_command_blocks_delete(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_ALLOW_DELETE", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_ALLOW_DELETE", raising=False)
    from aiforge_core.runtime import chat_agent
    out = chat_agent._t_run_command({"cmd": "rm -rf /tmp/x"}, str(tmp_path))
    assert out["ok"] is False and out.get("blocked") == "delete"
    # everything else runs
    ok = chat_agent._t_run_command({"cmd": "echo hi"}, str(tmp_path))
    assert ok["ok"] is True and "hi" in ok["stdout"]


# ── chat cancellation registry ───────────────────────────────────────
def test_chat_cancel_registry_and_active():
    from aiforge_core.runtime import chat_cancel
    tok = chat_cancel.start(99)
    chat_cancel.set_active(99)
    assert chat_cancel.active() == 99
    assert chat_cancel.is_cancelled(99) is False
    assert tok.cancelled is False
    assert chat_cancel.cancel(99) is True
    assert chat_cancel.is_cancelled(99) is True
    chat_cancel.finish(99)
    assert chat_cancel.get(99) is None
    # cancelling an unknown session is a no-op
    assert chat_cancel.cancel(12345) is False


def test_chat_run_command_stops_when_cancelled(tmp_path):
    from aiforge_core.runtime import chat_agent, chat_cancel
    chat_cancel.start(101)
    chat_cancel.set_active(101)
    chat_cancel.cancel(101)  # pre-cancelled
    out = chat_agent._t_run_command({"cmd": "sleep 30"}, str(tmp_path))
    assert out["ok"] is False and out.get("stopped") is True
    chat_cancel.finish(101)


# ── project runner stack detection + command plan ────────────────────
def test_project_detect_and_plan(tmp_path):
    from aiforge_core.runtime.tools import project_runner as pr
    (tmp_path / "pom.xml").write_text("<project/>")
    assert pr.detect(str(tmp_path))["stacks"] == ["maven"]
    tools, cmds = pr._plan("maven", "build", str(tmp_path))
    assert "mvn" in tools and any("package" in c for c in cmds)


def test_project_detect_node_react(tmp_path):
    from aiforge_core.runtime.tools import project_runner as pr
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "18"}}')
    stacks = pr.detect(str(tmp_path))["stacks"]
    assert stacks == ["node:react"]
    _, cmds = pr._plan("node:react", "run", str(tmp_path))
    assert cmds == ["npm run dev"]


def test_project_detect_python_and_go(tmp_path):
    from aiforge_core.runtime.tools import project_runner as pr
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "go.mod").write_text("module x\n")
    stacks = set(pr.detect(str(tmp_path))["stacks"])
    assert {"python", "go"} <= stacks


def test_project_tool_registered_on_doer():
    from aiforge_core.runtime.doer_tools import adk_function_tools
    names = {getattr(getattr(t, "func", None), "__name__", "")
             for t in adk_function_tools()}
    assert "project" in names
