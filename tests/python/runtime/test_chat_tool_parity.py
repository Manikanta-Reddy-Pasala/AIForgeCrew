"""Chat-agent tool parity with the pipeline Doer.

The pipeline Doer exposes four powerful tools the SIMPLE-CHAT agent historically
lacked: ``mcp``, ``browse`` (Playwright), ``execute_ipython_cell`` (Jupyter) and
``delegate_to_agent`` (sub-agent spawn). These tests assert the chat agent now
registers all four in its ``TOOLS`` dict and that each thin handler forwards the
right kwargs from ``args`` to the underlying pipeline tool.
"""
from __future__ import annotations

import aiforge_core.runtime.chat_agent as ca


def test_tools_dict_has_parity_keys():
    for key in ("mcp", "browse", "execute_ipython_cell", "delegate_to_agent",
                "delegate"):
        assert key in ca.TOOLS, f"missing chat tool: {key}"


def test_run_id_is_stable_per_cwd():
    # The browser tab / IPython kernel must persist across chat turns, so the
    # derived run id must be identical for the same cwd and differ across cwds.
    a1 = ca._chat_run_id("/work/proj-a")
    a2 = ca._chat_run_id("/work/proj-a")
    b1 = ca._chat_run_id("/work/proj-b")
    assert a1 == a2
    assert a1 != b1
    assert a1.startswith("chat-")


def test_mcp_handler_forwards_kwargs(monkeypatch):
    seen = {}

    def _fake_mcp(command, *, endpoint=None, tool=None, arguments=None):
        seen.update(command=command, endpoint=endpoint, tool=tool,
                    arguments=arguments)
        return {"ok": True}

    monkeypatch.setattr("aiforge_core.runtime.tools.mcp_client.mcp", _fake_mcp)
    out = ca.TOOLS["mcp"](
        {"command": "call_tool", "endpoint": "gh", "tool": "search",
         "arguments": {"q": "x"}}, "/cwd")
    assert out == {"ok": True}
    assert seen == {"command": "call_tool", "endpoint": "gh",
                    "tool": "search", "arguments": {"q": "x"}}


def test_browse_handler_forwards_kwargs_and_stable_run_id(monkeypatch):
    seen = {}

    def _fake_browse(command, *, url=None, path=None, selector=None, text=None,
                     x=None, y=None, button=None, key=None, dx=None, dy=None,
                     _run_id=None):
        seen.update(command=command, url=url, _run_id=_run_id)
        return {"ok": True}

    monkeypatch.setattr("aiforge_core.runtime.tools.browser.browse", _fake_browse)
    out = ca.TOOLS["browse"]({"command": "goto", "url": "http://x"}, "/cwd")
    assert out == {"ok": True}
    assert seen["command"] == "goto"
    assert seen["url"] == "http://x"
    assert seen["_run_id"] == ca._chat_run_id("/cwd")


def test_ipython_handler_forwards_code_and_run_id(monkeypatch):
    seen = {}

    def _fake_cell(code, *, timeout=None, _run_id=None):
        seen.update(code=code, timeout=timeout, _run_id=_run_id)
        return {"ok": True}

    monkeypatch.setattr(
        "aiforge_core.runtime.tools.ipython_kernel.execute_ipython_cell",
        _fake_cell)
    out = ca.TOOLS["execute_ipython_cell"](
        {"code": "print(1)", "timeout": 5}, "/cwd")
    assert out == {"ok": True}
    assert seen["code"] == "print(1)"
    assert seen["timeout"] == 5
    assert seen["_run_id"] == ca._chat_run_id("/cwd")


def test_delegate_handler_forwards_role_and_prompt(monkeypatch):
    seen = {}

    def _fake_delegate(role, prompt, *, timeout=600):
        seen.update(role=role, prompt=prompt, timeout=timeout)
        return {"ok": True}

    monkeypatch.setattr(
        "aiforge_core.runtime.tools.delegation.delegate_to_agent",
        _fake_delegate)
    out = ca.TOOLS["delegate_to_agent"](
        {"role": "tester", "prompt": "run tests", "timeout": 120}, "/cwd")
    assert out == {"ok": True}
    assert seen == {"role": "tester", "prompt": "run tests", "timeout": 120}
    # ``delegate`` alias points at the same handler.
    assert ca.TOOLS["delegate"] is ca.TOOLS["delegate_to_agent"]


def test_handlers_degrade_gracefully_when_dep_missing(monkeypatch):
    # If the underlying tool raises (e.g. playwright/jupyter not installed), the
    # handler must return a soft error, never propagate into the chat loop.
    def _boom(*a, **k):
        raise RuntimeError("dep missing")

    monkeypatch.setattr("aiforge_core.runtime.tools.browser.browse", _boom)
    out = ca.TOOLS["browse"]({"command": "goto", "url": "http://x"}, "/cwd")
    assert out["ok"] is False
    assert "dep missing" in out["error"]


def test_ipython_is_approval_gated_and_cwd_jailed():
    # execute_ipython_cell runs arbitrary code — exposing it to chat (F1) must
    # NOT leave it ungated/unsandboxed (the old design excluded it entirely).
    # It must be approval-gated (ASK in Act mode) and in the cwd-jailed set.
    from aiforge_core.runtime.tools import tool_policy
    assert tool_policy.decide("execute_ipython_cell")["policy"] == tool_policy.ASK
    assert "execute_ipython_cell" in ca._ROOT_SCOPED_TOOLS
