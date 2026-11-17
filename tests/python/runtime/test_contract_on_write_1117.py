"""The request's stated signatures are compared right after the class is written."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _contract_note as cn

GOAL = ("The unit test will call: `new com.acme.sync.Notifier(String url, String token)` "
        "and `void changed(String id)`.")


def _write(tmp_path, body):
    d = tmp_path / "src" / "main" / "java" / "com" / "acme" / "sync"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "Notifier.java"
    f.write_text("package com.acme.sync;\n" + body)
    return f


def test_gap_told_once_after_writing_the_class(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_CONTRACT_ON_WRITE", raising=False)
    held = []
    from aiforge_core.runtime.chat_agent._turn import _notes
    monkeypatch.setattr(_notes, "hold", lambda st, m: held.append(m))
    f = _write(tmp_path, "public class Notifier {\n public Notifier(Object client) {}\n"
                         " public void changed(String id) {}\n}\n")
    st = SimpleNamespace(goal=GOAL, cwd=str(tmp_path))
    ev = list(cn.check(st, [("Notifier.java", str(f))], str(tmp_path)))
    assert ev and held and "Notifier(String url, String token)" in held[0]
    assert list(cn.check(st, [("Notifier.java", str(f))], str(tmp_path))) == []   # once


def test_no_note_when_it_matches_or_another_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_CONTRACT_ON_WRITE", raising=False)
    f = _write(tmp_path, "public class Notifier {\n public Notifier(String url, String token) {}\n"
                         " public void changed(String id) {}\n}\n")
    st = SimpleNamespace(goal=GOAL, cwd=str(tmp_path))
    assert list(cn.check(st, [("Notifier.java", str(f))], str(tmp_path))) == []
    other = tmp_path / "Other.java"
    other.write_text("class Other {}\n")
    assert list(cn.check(st, [("Other.java", str(other))], str(tmp_path))) == []


def test_a_method_gap_on_the_written_class_is_told(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_CONTRACT_ON_WRITE", raising=False)
    held = []
    from aiforge_core.runtime.chat_agent._turn import _notes
    monkeypatch.setattr(_notes, "hold", lambda st, m: held.append(m))
    goal = ("The test will call `Mono<Void> keep(com.acme.Settings incoming, String name)` "
            "on `com.acme.sync.SyncImpl`.")
    d = tmp_path / "src" / "main" / "java" / "com" / "acme" / "sync"
    d.mkdir(parents=True)
    f = d / "SyncImpl.java"
    f.write_text("package com.acme.sync;\nclass SyncImpl {\n"
                 " Mono<Void> keep(Settings incoming, String name, String action) { return null; }\n}\n")
    st = SimpleNamespace(goal=goal, cwd=str(tmp_path))
    assert list(cn.check(st, [("SyncImpl.java", str(f))], str(tmp_path)))
    assert held and "keep(" in held[0]
