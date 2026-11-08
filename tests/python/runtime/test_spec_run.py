"""Before the answer, the request's own call examples are run and the changed
modules imported, the way the turn's tests run."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _spec_run as S

SPEC = ('Add `split(text)` to `pkg/mod.py`. Today `split("a, b")` returns `None`, which is wrong. '
        'Examples: `split("a, b")` -> `["a", "b"]`; '
        '`split("")` returns `[]`; `split("x") -> ["x"]`. Also `pkg.mod.split(" y ")` gives `["y"]`.'
        ' Not literal: `split(obj)` -> `[obj]`. A tuple: `split("t")` -> `("t",)`.')


def test_call_examples_are_read_from_the_request():
    got = {e["call"]: e["want"] for e in S.examples(SPEC)}
    # the "today ... wrong" statement is not an example; X.attr calls and
    # tuples (not JSON-safe) are skipped
    assert got == {'split("a, b")': ["a", "b"], 'split("")': [], 'split("x")': ["x"]}


def _repo(tmp_path, body):
    (tmp_path / "pkg").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text("X = 1\n")
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                              "commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    (tmp_path / "pkg" / "mod.py").write_text(body)
    return SimpleNamespace(goal=SPEC, head0=head, cwd=str(tmp_path), edits_made=1, convo=[],
                           readonly_mode=False, plan_mode=False,
                           last_test_cmd=("run_command",
                                          {"cmd": f"{sys.executable} -m pytest tests -q"}))


def test_a_wrong_example_and_a_broken_import_send_the_answer_back(tmp_path):
    st = _repo(tmp_path, "def split(text):\n    return text.split(',')\n")
    fails = S.run(st, str(tmp_path))
    whats = {f["what"] for f in fails}
    assert 'split("a, b")' in whats and 'split("")' in whats and 'split("x")' not in whats
    step = {"text": "FINAL: done"}
    st.spec_run_done = False                  # the run above used the turn's one go
    g = S.gate(st, step, None, False)
    events = []
    try:
        while True:
            events.append(next(g))
    except StopIteration as stop:
        sig = stop.value
    assert sig == "continue" and "should give" in st.convo[-1]["content"]
    assert list(S.gate(st, step, None, False)) == []                     # once per turn
    st2 = _repo(tmp_path / "b", "from pkg.nothere import split\n")
    (tmp_path / "b" / "pkg" / "__init__.py").write_text("from .mod import split\n")
    subprocess.run(["git", "-C", str(tmp_path / "b"), "add", "-A"], capture_output=True)
    fails = S.run(st2, str(tmp_path / "b"))
    assert any(f["kind"] == "import" and f["what"] == "pkg" for f in fails)


def test_a_script_with_side_effects_is_never_imported(tmp_path):
    st = _repo(tmp_path, "def split(text):\n    return [p.strip() for p in text.split(',') if p.strip()]\n")
    marker = tmp_path / "RAN"
    (tmp_path / "deploy.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True)
    assert S.run(st, str(tmp_path)) == [] and not marker.exists()


def test_correct_code_passes_quietly(tmp_path):
    st = _repo(tmp_path, "def split(text):\n    return [p.strip() for p in text.split(',') if p.strip()]\n")
    assert S.run(st, str(tmp_path)) == []


def test_not_without_edits_a_test_command_or_when_off(tmp_path, monkeypatch):
    st = _repo(tmp_path, "def split(text):\n    return 1\n")
    st.last_test_cmd = None
    assert S.run(st, str(tmp_path)) == []
    st = _repo(tmp_path / "c", "def split(text):\n    return 1\n")
    st.edits_made = 0
    assert list(S.gate(st, {}, None, False)) == []
    monkeypatch.setenv("AIFORGE_CHAT_SPEC_RUN", "0")
    st.edits_made = 1
    assert list(S.gate(st, {}, None, False)) == []
