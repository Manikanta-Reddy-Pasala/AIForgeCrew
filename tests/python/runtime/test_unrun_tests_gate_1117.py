"""A new JVM 'test' the build never runs (no @Test) is sent back once."""
import os
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _test_guard as T


def _repo(tmp_path, pom="<project/>"):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (tmp_path / "pom.xml").write_text(pom)
    for c in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True, env=env)
    base = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    t = tmp_path / "src" / "test" / "java" / "a" / "DetectorTest.java"
    t.parent.mkdir(parents=True)
    return base, t


def _run(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return stop.value, events


def test_main_method_test_is_sent_back_with_the_dependency(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_UNRUN_TESTS_GATE", raising=False)
    from aiforge_core.runtime.chat_agent._turn import _env_guard
    monkeypatch.setattr(_env_guard, "_m2_has", lambda g, a: True)
    base, t = _repo(tmp_path)
    t.write_text("package a;\npublic class DetectorTest { public static void main(String[] a) {} }")
    st = SimpleNamespace(cwd=str(tmp_path), edits_made=2, goal="Detect it. Fix it with a unit test.",
                         head0=base, convo=[])
    sig, events = _run(T.unrun_tests_gate(st, {}))
    assert sig == "continue" and "@Test" in st.convo[-1]["content"]
    assert "junit-jupiter" in st.convo[-1]["content"]
    assert _run(T.unrun_tests_gate(st, {}))[0] is None                   # once


def test_a_real_junit_test_or_no_test_request_passes(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_UNRUN_TESTS_GATE", raising=False)
    base, t = _repo(tmp_path)
    t.write_text("package a;\nimport org.junit.jupiter.api.Test;\nclass DetectorTest { @Test void x() {} }")
    st = SimpleNamespace(cwd=str(tmp_path), edits_made=2, goal="Fix it with a unit test.", head0=base, convo=[])
    assert _run(T.unrun_tests_gate(st, {}))[0] is None
    t.write_text("package a;\npublic class DetectorTest { public static void main(String[] a) {} }")
    st2 = SimpleNamespace(cwd=str(tmp_path), edits_made=2, goal="Fix the detector.", head0=base, convo=[])
    assert _run(T.unrun_tests_gate(st2, {}))[0] is None                  # no tests asked
