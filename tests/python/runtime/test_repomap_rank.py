"""Ranked repo map: PageRank centrality, personalisation, window-scaled budget,
byte-stable cache, soft-fail to the old map."""
from __future__ import annotations

import threading

import pytest

from aiforge_core.runtime.chat_agent._context import _repomap as rm
from aiforge_core.runtime.chat_agent._context import _repomap_rank as rr


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    rr.reset_caches()
    monkeypatch.setattr(rm, "_workspace_root", lambda: None)
    monkeypatch.delenv("AIFORGE_REPOMAP_RANK", raising=False)
    yield
    rr.reset_caches()


@pytest.fixture
def repo(tmp_path):
    """core.py is used by everything; leaf_*.py use it and nobody uses them."""
    (tmp_path / "core.py").write_text(
        "def central_helper():\n    pass\n\nclass CoreEngine:\n    pass\n")
    for n in ("alpha", "beta", "gamma", "delta"):
        (tmp_path / f"leaf_{n}.py").write_text(
            f"from core import central_helper\n\ndef run_{n}():\n"
            "    central_helper()\n    CoreEngine()\n")
    (tmp_path / "lonely.py").write_text("def lonely_func():\n    pass\n")
    return str(tmp_path)


def _order(text):
    """File order in the map: symbol lines first, then the names-only list."""
    out = []
    for ln in text.splitlines():
        if ln.startswith("Other files"):
            out += [x.strip() for x in ln.split(":", 1)[1].split(",")]
        elif ".py: " in ln:
            out.append(ln.split(":")[0])
    return out


def test_central_file_outranks_leaf(repo):
    names = _order(rr.ranked_map(repo, budget=20000))
    assert names[0] == "core.py"
    assert names.index("core.py") < names.index("leaf_alpha.py")


def test_personalization_boosts_read_file(repo):
    plain = _order(rr.ranked_map(repo, budget=20000, session_id="other"))
    rr.note_focus("s", repo, [repo + "/lonely.py"], 2.0)
    boosted = _order(rr.ranked_map(repo, budget=20000, session_id="s"))
    assert boosted.index("lonely.py") < plain.index("lonely.py")


def test_user_text_mentions_boost(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPOMAP_REFRESH_DELTA", "1")
    plain = _order(rr.ranked_map(repo, budget=20000))
    out = _order(rr.ranked_map(repo, budget=20000,
                               user_text="look at @lonely.py and lonely_func"))
    assert out.index("lonely.py") < plain.index("lonely.py")


def test_budget_scales_with_window(monkeypatch):
    from aiforge_core.runtime.chat_agent._context import _window as w
    monkeypatch.delenv("AIFORGE_REPOMAP_MAX_CHARS", raising=False)
    monkeypatch.setattr(w, "_window_tokens", lambda role=None: 32768)
    monkeypatch.setattr(w, "_resolved_window", lambda role=None: 32768)
    small = rr.map_budget_chars()
    monkeypatch.setattr(w, "_window_tokens", lambda role=None: 262144)
    monkeypatch.setattr(w, "_resolved_window", lambda role=None: 262144)
    big = rr.map_budget_chars()
    assert big > small
    # a long history eats the free space
    assert rr.map_budget_chars(history_chars=big * 100) < big
    # small history differences do not move the budget (cache stability)
    assert rr.map_budget_chars(history_chars=10) == rr.map_budget_chars(history_chars=500)


def test_explicit_cap_wins(monkeypatch):
    monkeypatch.setenv("AIFORGE_REPOMAP_MAX_CHARS", "3000")
    assert rr.map_budget_chars() <= 3000
    monkeypatch.setenv("AIFORGE_REPOMAP_MAX_CHARS", "0")
    assert rr.map_budget_chars() == 0


def test_low_ranked_files_are_names_only(tmp_path):
    for i in range(40):
        (tmp_path / f"mod_{i:02d}.py").write_text(
            f"def func_number_{i}_alpha():\n    pass\n\ndef func_number_{i}_beta():\n    pass\n")
    out = rr.ranked_map(str(tmp_path), budget=1500)
    assert len(out) <= 1600
    assert "Other files (names only):" in out
    assert out.count(": func_") < 40


def test_same_inputs_identical_text_and_follow_up_stable(repo):
    kw = dict(budget=20000, session_id="s", user_text="fix core")
    a = rr.ranked_map(repo, **kw)
    assert rr.ranked_map(repo, **kw) == a
    rr.note_focus("s", repo, [repo + "/lonely.py"], 1.0)      # below the delta
    assert rr.ranked_map(repo, **kw) == a
    rr.note_focus("s", repo, [repo + f"/leaf_{n}.py" for n in ("alpha", "beta", "gamma")], 2.0)
    assert rr.ranked_map(repo, **kw) != a                      # material change


def test_cache_cold_equals_warm(repo):
    a = rr.ranked_map(repo, budget=20000, session_id="x")
    rr.reset_caches()
    assert rr.ranked_map(repo, budget=20000, session_id="x") == a


def test_off_switch_is_old_behaviour(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPOMAP_RANK", "0")
    assert rr.ranked_map(repo, budget=20000) == ""
    out = rm._build_repo_map(repo)
    assert "files ranked" not in out and "core.py" in out


def test_build_repo_map_uses_rank_by_default(repo):
    out = rm._build_repo_map(repo, focus={"history_chars": 0})
    assert "files ranked" in out and "central_helper" in out


def test_soft_fail_to_old_map(repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(rr, "scan_repo", boom)
    out = rm._build_repo_map(repo)
    assert "files ranked" not in out and "core.py" in out


def test_timeout_falls_back_and_scan_continues(repo, monkeypatch):
    gate = threading.Event()
    real = rr.scan_repo

    def slow(base, deadline=None):
        gate.wait(5)
        return real(base, deadline)
    monkeypatch.setattr(rr, "scan_repo", slow)
    monkeypatch.setenv("AIFORGE_REPOMAP_BUDGET_S", "0.1")
    out = rm._build_repo_map(repo)
    assert "files ranked" not in out and "core.py" in out      # old map, not blocked
    gate.set()
    rr._JOBS[repo]["thread"].join(5)
    assert "files ranked" in rm._build_repo_map(repo)           # next turn: ranked


def test_scan_is_incremental(repo):
    s1 = rr.scan_repo(repo)
    s2 = rr.scan_repo(repo)
    assert s2["core.py"] is s1["core.py"]


def test_pagerank_basic():
    r = rr.pagerank(3, {0: {2: 1.0}, 1: {2: 1.0}})
    assert r[2] > r[0] and abs(sum(r) - 1) < 1e-6
    assert rr.pagerank(0, {}) == []
