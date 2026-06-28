"""Pure-unit tests for deterministic domain/flow computation (no Neo4j, no LM)."""
from __future__ import annotations

from aiforge_memory.features.domain import extract as dx


def test_connected_components_groups_and_singletons():
    comps = dx._connected_components(
        ["a", "b", "c", "d"], [("a", "b"), ("b", "c")])
    # {a,b,c} together, d alone; largest first
    assert comps[0] == ["a", "b", "c"]
    assert ["d"] in comps


def test_entry_symbols_picks_no_incoming_and_markers():
    symbols = [
        {"fqname": "app.ApiController.handle", "kind": "method"},
        {"fqname": "svc.Worker.run", "kind": "method"},
        {"fqname": "util.helper", "kind": "function"},
    ]
    calls = [("app.ApiController.handle", "svc.Worker.run"),
             ("svc.Worker.run", "util.helper")]
    entries = dx._entry_symbols(symbols, calls)
    # ApiController is a marker AND has no incoming → first
    assert entries[0] == "app.ApiController.handle"
    # util.helper has incoming and no marker → not an entry
    assert "util.helper" not in entries


def test_bfs_chain_ordered_and_depth_capped():
    adj = {"a": ["b", "c"], "b": ["d"], "c": ["d"]}
    chain = dx._bfs_chain("a", adj, depth=3)
    assert chain[0] == "a"
    assert len(chain) == 3
    assert chain[1] in ("b", "c")


def test_compute_builds_domains_and_flows():
    services = [{"name": "api"}, {"name": "core"}, {"name": "lonely"}]
    svc_edges = [("api", "core")]               # api+core one domain; lonely alone
    symbols = [
        {"fqname": "api.Ctrl.handle", "kind": "controller", "service": "api"},
        {"fqname": "core.Svc.do", "kind": "method", "service": "core"},
        {"fqname": "core.Repo.save", "kind": "method", "service": "core"},
    ]
    call_edges = [("api.Ctrl.handle", "core.Svc.do"),
                  ("core.Svc.do", "core.Repo.save")]
    domains, flows = dx._compute(services, svc_edges, symbols, call_edges)

    names = {tuple(d.services) for d in domains}
    assert ("api", "core") in names or ("core", "api") in {tuple(sorted(d.services)) for d in domains}
    assert any(d.services == ["lonely"] for d in domains)
    # one flow from the controller entry, ordered
    assert flows, "expected at least one flow"
    f = flows[0]
    assert f.steps[0]["node_id"] == "api.Ctrl.handle"
    assert [s["order"] for s in f.steps] == list(range(len(f.steps)))


def test_extract_domains_naming_off_uses_deterministic(monkeypatch):
    # extract_domains with naming=False must not call the LM
    called = {"n": 0}
    monkeypatch.setattr(dx, "_call_llm",
                        lambda **k: called.__setitem__("n", called["n"] + 1) or "{}")

    class _Sess:
        def run(self, q, **kw):
            return []
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Drv:
        def session(self): return _Sess()

    res = dx.extract_domains(_Drv(), repo="r", naming=False)
    assert called["n"] == 0
    assert res.repo == "r"
