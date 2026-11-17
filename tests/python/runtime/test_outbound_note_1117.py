"""A note when the work adds a call to another service, once per turn."""
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _outbound_note as ob


def _repo(tmp_path):
    r = tmp_path / "r"
    (r / "src").mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "src" / "Old.java").write_text("class Old { WebClient c; }\n")
    (r / "src" / "Plain.java").write_text("class Plain {}\n")
    g("add", "."); g("commit", "-qm", "base")
    rev = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    return r, rev


def _run(st, files, cwd):
    return list(ob.check(st, files, cwd))


def test_note_once_when_a_file_gains_an_outbound_call(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_OUTBOUND_NOTE", raising=False)
    r, rev = _repo(tmp_path)
    held = []
    from aiforge_core.runtime.chat_agent._turn import _notes
    monkeypatch.setattr(_notes, "hold", lambda st, msg: held.append(msg))
    st = SimpleNamespace(cwd=str(r), head0=rev)
    new = r / "src" / "Notifier.java"
    new.write_text("class Notifier { void go() { WebClient.builder().build(); } }\n")
    ev = _run(st, [("Notifier.java", str(new))], str(r))
    assert ev and held and "Not configured" in held[0]
    assert _run(st, [("Notifier.java", str(new))], str(r)) == []          # once per turn


def test_no_note_for_existing_calls_tests_or_plain_code(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_OUTBOUND_NOTE", raising=False)
    r, rev = _repo(tmp_path)
    st = SimpleNamespace(cwd=str(r), head0=rev)
    old = r / "src" / "Old.java"
    old.write_text("class Old { WebClient c; int x; }\n")                 # had a call already
    plain = r / "src" / "Plain.java"
    plain.write_text("class Plain { int y; }\n")
    t = r / "src" / "test" / "NotifierTest.java"
    t.parent.mkdir()
    t.write_text("class NotifierTest { HttpClient h; }\n")
    assert _run(st, [("Old.java", str(old)), ("Plain.java", str(plain)),
                     ("NotifierTest.java", str(t))], str(r)) == []
    monkeypatch.setenv("AIFORGE_CHAT_OUTBOUND_NOTE", "0")
    new = r / "src" / "N.py"
    new.write_text("import requests\nrequests.post(url)\n")
    assert _run(st, [("N.py", str(new))], str(r)) == []
