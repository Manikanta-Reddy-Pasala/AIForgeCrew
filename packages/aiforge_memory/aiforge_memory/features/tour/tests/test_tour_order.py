"""Pure-unit tests for deterministic tour ordering + markdown render."""
from __future__ import annotations

from aiforge_memory.features.tour import extract as tx


def _syms():
    return [
        {"fqname": "app.ApiController.handle", "kind": "controller", "note": "entry"},
        {"fqname": "core.Svc.do", "kind": "method", "note": ""},
        {"fqname": "core.Repo.save", "kind": "method", "note": ""},
        {"fqname": "util.orphan", "kind": "function", "note": ""},
    ]


def _calls():
    return [("app.ApiController.handle", "core.Svc.do"),
            ("core.Svc.do", "core.Repo.save")]


def test_order_starts_at_entry_and_is_deduped():
    order = tx._order(_syms(), _calls())
    assert order[0] == "app.ApiController.handle"
    assert order.index("core.Svc.do") < order.index("core.Repo.save")
    assert len(order) == len(set(order))         # no dups
    assert "util.orphan" in order                # leftovers appended


def test_order_respects_domain_allowlist():
    allow = {"core.Svc.do", "core.Repo.save"}
    order = tx._order(_syms(), _calls(), allow=allow)
    assert set(order) <= allow
    assert "app.ApiController.handle" not in order


def test_order_caps_at_max_stops():
    order = tx._order(_syms(), _calls(), max_stops=2)
    assert len(order) == 2


def test_render_md_numbers_and_notes():
    tour = tx.TourDraft(repo="r", stops=[
        {"node_id": "a.b", "kind": "symbol", "label": "a.b", "order": 0, "note": "start here"},
        {"node_id": "c.d", "kind": "symbol", "label": "c.d", "order": 1, "note": ""},
    ])
    md = tx.render_md(tour)
    assert "# Tour: r" in md
    assert "1. **a.b**" in md
    assert "start here" in md
    assert "2. **c.d**" in md


def test_build_tour_naming_off_no_llm(monkeypatch):
    import aiforge_memory.features.domain.extract as dx
    monkeypatch.setattr(dx, "_call_llm",
                        lambda **k: (_ for _ in ()).throw(AssertionError("LM called")))

    class _Sess:
        def run(self, q, **kw):
            return []
        def single(self): return None
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Drv:
        def session(self): return _Sess()

    t = tx.build_tour(_Drv(), repo="r", naming=False)
    assert t.repo == "r" and t.stops == []
