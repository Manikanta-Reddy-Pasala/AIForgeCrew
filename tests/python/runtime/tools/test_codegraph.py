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


def test_round10_fast_path_rebuilds_on_corrupt_index(tmp_path, monkeypatch):
    """A corrupt index left by a CRASHED prior process must not be trusted
    forever: ensure_indexed verifies once, removes the corrupt index, rebuilds."""
    from aiforge_core.runtime.tools import codegraph as cg
    repo = tmp_path
    d = repo / ".codegraph"
    d.mkdir()
    (d / "graph.db").write_text("not a real sqlite file")   # corrupt

    monkeypatch.setattr(cg, "_autobuild_enabled", lambda: True)
    monkeypatch.setattr(cg, "_disabled", lambda: False)
    monkeypatch.setattr(cg, "available", lambda: True)
    monkeypatch.setattr(cg, "_bin", lambda: "/usr/bin/true")
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(repo))
    cg._VERIFIED_HEALTHY.clear()
    cg._FAILED.clear()

    built = {"n": 0}

    def fake_run(cmd, **k):
        built["n"] += 1
        d.mkdir(exist_ok=True)                                # corrupt was rm'd
        (d / "graph.db").write_bytes(_valid_sqlite_bytes())   # good rebuild

        class _P:
            returncode = 0
            stdout = stderr = ""
        return _P()
    monkeypatch.setattr(cg.subprocess, "run", fake_run)

    assert cg.ensure_indexed(str(repo)) is True
    assert built["n"] == 1                      # corrupt index triggered rebuild
    # second call trusts the now-healthy cached fast-path — no rebuild
    assert cg.ensure_indexed(str(repo)) is True
    assert built["n"] == 1


def test_round10_fast_path_trusts_healthy_index(tmp_path, monkeypatch):
    from aiforge_core.runtime.tools import codegraph as cg
    repo = tmp_path
    d = repo / ".codegraph"
    d.mkdir()
    (d / "graph.db").write_bytes(_valid_sqlite_bytes())

    monkeypatch.setattr(cg, "_autobuild_enabled", lambda: True)
    monkeypatch.setattr(cg, "_disabled", lambda: False)
    monkeypatch.setattr(cg, "available", lambda: True)
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(repo))
    cg._VERIFIED_HEALTHY.clear()

    def no_build(cmd, **k):
        raise AssertionError("healthy index must not rebuild")
    monkeypatch.setattr(cg.subprocess, "run", no_build)
    assert cg.ensure_indexed(str(repo)) is True


def _valid_sqlite_bytes() -> bytes:
    import sqlite3
    import tempfile
    import os
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    with open(p, "rb") as f:
        data = f.read()
    os.unlink(p)
    return data


def test_round11_corrupt_index_not_deleted_when_build_contended(tmp_path, monkeypatch):
    """Regression: the fast-path must NOT rmtree a corrupt-reading index without
    holding the cross-process lock — it could be a CONCURRENT builder's DB caught
    mid-write. When the build lock is contended, ensure_indexed returns False and
    leaves the index on disk (the lock holder will finish/replace it)."""
    from aiforge_core.runtime.tools import codegraph as cg
    repo = tmp_path
    d = repo / ".codegraph"
    d.mkdir()
    (d / "graph.db").write_text("torn mid-write header")   # reads corrupt

    monkeypatch.setattr(cg, "_autobuild_enabled", lambda: True)
    monkeypatch.setattr(cg, "_disabled", lambda: False)
    monkeypatch.setattr(cg, "available", lambda: True)
    monkeypatch.setattr(cg, "_bin", lambda: "/usr/bin/true")
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(repo))
    cg._VERIFIED_HEALTHY.clear()
    cg._FAILED.clear()
    # simulate: another process holds the build lock (contended)
    monkeypatch.setattr(cg, "_acquire_build_lock", lambda repo: None)
    monkeypatch.setattr(cg.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not build while contended")))

    assert cg.ensure_indexed(str(repo)) is False
    assert (d / "graph.db").exists()       # concurrent builder's DB left intact
