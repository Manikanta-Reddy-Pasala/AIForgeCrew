"""Unit tests for domain/flow wiring in the context bundle (no Neo4j)."""
from __future__ import annotations

from aiforge_memory.query import bundle as b


def test_render_includes_domains_and_flows():
    cb = b.ContextBundle(repo="r")
    cb.domains = [{"name": "Auth & Tokens", "description": "login + refresh",
                   "services": ["api", "auth"]}]
    cb.flows = [{"name": "flow:login", "description": "",
                 "steps": ["Ctrl.handle", "Svc.do", "Repo.save"]}]
    md = cb.render()
    assert "## Domains" in md
    assert "Auth & Tokens" in md and "[api, auth]" in md
    assert "## Flows" in md
    assert "Ctrl.handle → Svc.do → Repo.save" in md


def test_domains_and_flows_helpers_parse_rows():
    class _Sess:
        def __init__(self, rows): self.rows = rows
        def run(self, q, **kw):
            return iter(self.rows)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Drv:
        def __init__(self, rows): self.rows = rows
        def session(self): return _Sess(self.rows)

    dom = b._domains_for(_Drv([{"name": "D", "description": "x",
                                "services": ["a"]}]), repo="r")
    assert dom[0]["name"] == "D" and dom[0]["services"] == ["a"]

    fl = b._flows_for(_Drv([{"name": "F", "description": "",
                             "steps": ["s1", "s2"]}]),
                      repo="r", fqnames=["x"])
    assert fl[0]["steps"] == ["s1", "s2"]
    # empty fqnames short-circuits to []
    assert b._flows_for(_Drv([]), repo="r", fqnames=[]) == []
