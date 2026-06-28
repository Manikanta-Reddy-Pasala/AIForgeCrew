"""Pure-unit tests for deterministic graph-completeness findings + render."""
from __future__ import annotations

from aiforge_memory.features.review import extract as rx


def test_findings_only_nonempty_and_severity_sorted():
    raw = {
        "files_no_symbols": ["a.py", "b.py"],
        "symbols_no_edges": ["x.f"],
        "services_no_files": ["ghost"],
        "files_no_summary": [],            # empty → no finding
        "services_no_domain": [],
    }
    fs = rx._findings(raw)
    kinds = [f.kind for f in fs]
    assert "files_no_summary" not in kinds      # empty dropped
    assert kinds[0] == "services_no_files"       # high severity first
    assert all(f.count == len(f.sample) or f.count >= len(f.sample) for f in fs)


def test_findings_sample_capped():
    raw = {"symbols_no_edges": [f"s{i}" for i in range(50)]}
    fs = rx._findings(raw)
    assert fs[0].count == 50
    assert len(fs[0].sample) == 5               # _SAMPLE cap


def test_render_clean_when_no_findings():
    rep = rx.ReviewReport(repo="r", findings=[],
                          totals={"services": 1, "files": 2, "symbols": 3})
    md = rx.render_md(rep)
    assert "No coverage gaps found" in md
    assert "symbols=3" in md


def test_render_lists_findings():
    rep = rx.ReviewReport(repo="r", findings=rx._findings(
        {"services_no_files": ["ghost"]}), totals={})
    md = rx.render_md(rep)
    assert "services_no_files" in md
    assert "ghost" in md


def test_review_graph_naming_off_no_llm(monkeypatch):
    import aiforge_memory.features.domain.extract as dx
    monkeypatch.setattr(dx, "_call_llm",
                        lambda **k: (_ for _ in ()).throw(AssertionError("LM")))

    class _Rec(dict):
        def single(self):  # _Q_TOTALS path
            return {"services": 0, "files": 0, "symbols": 0}

    class _Sess:
        def run(self, q, **kw):
            return _Rec()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Drv:
        def session(self): return _Sess()

    rep = rx.review_graph(_Drv(), repo="r", naming=False)
    assert rep.repo == "r" and rep.findings == []
