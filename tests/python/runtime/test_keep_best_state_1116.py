"""A turn stopped by its limit never hands back code that tests worse than
its best tested state.

Live case: a real OrderService turn reached its best, kept editing, and
was stopped at 4 failing where it started with 1 (13/18 vs 16/18).
"""
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _best_state as B


def _repo(tmp_path):
    subprocess.run("git init -q && git -c user.email=a@b -c user.name=b commit -q --allow-empty -m x",
                   shell=True, cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("v0\n")
    subprocess.run("git add -A && git -c user.email=a@b -c user.name=b commit -q -m a",
                   shell=True, cwd=tmp_path, check=True)
    return SimpleNamespace(cwd=str(tmp_path), edits_made=0)


def test_worse_final_state_is_restored(tmp_path):
    st = _repo(tmp_path)
    (tmp_path / "a.py").write_text("best\n")
    (tmp_path / "new.py").write_text("helper-best\n")
    st.edits_made = 2
    B.note(st, "mvn -o -q test -Dtest=X 2>&1 | tail -40", 1, str(tmp_path))
    (tmp_path / "a.py").write_text("worse\n")
    (tmp_path / "new.py").write_text("helper-worse\n")
    st.edits_made = 5
    B.note(st, "mvn -o -q test -Dtest=X", 4, str(tmp_path))      # fresh run, worse
    evs = list(B.restore_if_worse(st))
    assert evs and "restored" in evs[0]["text"]
    assert (tmp_path / "a.py").read_text() == "best\n"
    assert (tmp_path / "new.py").read_text() == "helper-best\n"


def test_better_or_equal_final_state_is_kept(tmp_path):
    st = _repo(tmp_path)
    (tmp_path / "a.py").write_text("first\n")
    st.edits_made = 1
    B.note(st, "pytest -q", 2, str(tmp_path))
    (tmp_path / "a.py").write_text("final\n")
    st.edits_made = 3
    B.note(st, "pytest -q", 2, str(tmp_path))
    assert list(B.restore_if_worse(st)) == []
    assert (tmp_path / "a.py").read_text() == "final\n"


def test_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_KEEP_BEST", "0")
    st = _repo(tmp_path)
    B.note(st, "pytest", 1, str(tmp_path))
    assert not getattr(st, "best_state", None)


def test_give_up_restores_the_best_state(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn._stuck import ladder
    st = _repo(tmp_path)
    (tmp_path / "a.py").write_text("best\n")
    st.edits_made = 1
    B.note(st, "mvn -o test -Dtest=X", 2, str(tmp_path))
    (tmp_path / "a.py").write_text("does not compile\n")
    st.edits_made = 4
    B.note(st, "mvn -o test -Dtest=X", 999, str(tmp_path))

    def _spent(st, why):
        if False:
            yield None
        return "wrap_up"
    monkeypatch.setattr(ladder, "escalate", _spent)
    monkeypatch.setattr(ladder, "give_up_message", lambda st: "(stopped)")
    st.convo = []
    evs = list(ladder.change_approach(st, "stuck"))
    assert any("restored" in str(e.get("text", "")) for e in evs)
    assert (tmp_path / "a.py").read_text() == "best\n"
