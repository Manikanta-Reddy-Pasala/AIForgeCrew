"""The wrap-up keeps the request's contract; a stuck own test gets a 'simplify' hint."""
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _escalate as es


def _repo(tmp_path):
    r = tmp_path / "r"
    (r / "src" / "test").mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "README").write_text("x\n")
    g("add", "."); g("commit", "-qm", "base")
    return r


def test_wrap_up_names_the_missing_contract(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _contract_gate
    monkeypatch.setattr(_contract_gate, "_gaps",
                        lambda st, cached=True: ["constructor Notifier(String url, String token)"])
    assert es._contract_gaps(SimpleNamespace()) == ["constructor Notifier(String url, String token)"]
    monkeypatch.setattr(_contract_gate, "_gaps", lambda st, cached=True: 1 / 0)
    assert es._contract_gaps(SimpleNamespace()) == []


def test_own_test_hint_only_for_a_new_test_named_in_the_failure(tmp_path):
    r = _repo(tmp_path)
    (r / "src" / "test" / "NotifierTest.java").write_text("class NotifierTest {}\n")
    st = SimpleNamespace(cwd=str(r))
    assert es._own_test_hint(st, "same failure (build: Other.java: cannot find symbol)") == ""
    h = es._own_test_hint(st, "same failure (build:src/test/NotifierTest.java: cannot find symbol)")
    assert "NotifierTest.java" in h and "simplest test" in h
    assert es._own_test_hint(st, "same failure (NotifierTest)") == ""      # once
