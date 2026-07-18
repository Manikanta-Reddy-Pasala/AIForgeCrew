"""CodeGraph tool — CLI wrapper for code relations (callers/impact/explore)."""
from __future__ import annotations

from aiforge_core.runtime import chat_agent
from aiforge_core.runtime.tools import codegraph as cg


def test_registered_and_readonly():
    for t in ("codegraph_query", "codegraph_callers", "codegraph_callees",
              "codegraph_impact", "codegraph_explore"):
        assert t in chat_agent.TOOLS
        assert t in chat_agent._READONLY_TOOLS      # queries never gate


def test_missing_args_soft_error():
    assert cg.codegraph_query({}, "/tmp")["ok"] is False
    assert cg.codegraph_impact({}, "/tmp")["ok"] is False


def test_no_binary_soft_error(monkeypatch):
    monkeypatch.setattr(cg, "_bin", lambda: None)
    r = cg.codegraph_callers({"symbol": "Foo"}, "/tmp")
    assert r["ok"] is False and "not found" in r["error"]


def test_builds_cmd_with_path(monkeypatch):
    seen = {}

    class _P:
        returncode = 0
        stdout = "callers of Foo:\n- Bar.baz"
        stderr = ""

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return _P()
    monkeypatch.setattr(cg, "_bin", lambda: "/usr/bin/codegraph")
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", "/repo/x")
    monkeypatch.setattr(cg.subprocess, "run", fake_run)
    r = cg.codegraph_callers({"symbol": "Foo"}, "/cwd")
    assert r["ok"] and "Bar.baz" in r["result"]
    assert seen["cmd"] == ["/usr/bin/codegraph", "callers", "Foo", "--path", "/repo/x"]


def test_enabled_for_run_requires_index(tmp_path, monkeypatch):
    """The single gate: binary + real .codegraph index + not disabled + not
    opted out. Binary alone is NOT enough."""
    from aiforge_core.runtime.tools import codegraph as cg
    monkeypatch.setattr(cg, "_bin", lambda: "/usr/bin/true")
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CODEGRAPH_DISABLE", raising=False)
    monkeypatch.delenv("AIFORGE_CURRENT_TICKET", raising=False)
    assert cg.enabled_for_run() is False          # no .codegraph yet
    d = tmp_path / ".codegraph"
    d.mkdir()
    assert cg.enabled_for_run() is False           # EMPTY stub dir is not a real index
    (d / "graph.db").write_text("x")               # populated → real index
    assert cg.enabled_for_run() is True
    monkeypatch.setenv("AIFORGE_CODEGRAPH_DISABLE", "1")
    assert cg.enabled_for_run() is False           # env-disabled
