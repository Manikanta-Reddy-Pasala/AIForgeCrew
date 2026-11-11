"""Stub detection across languages, and the chat-side note: a function an
edit leaves as "not implemented" is named at once and in the turn's facts."""
import subprocess
import types

import pytest

from aiforge_core.runtime import stubs


@pytest.mark.parametrize("path,text,name", [
    ("m.py", "def f(x):\n    raise NotImplementedError\n", "f"),
    ("m.py", "class C:\n    def get(self):\n        \"\"\"Doc.\"\"\"\n"
             "        raise NotImplementedError('later')\n", "C.get"),
    ("m.py", "def f():\n    ...\n", "f"),
    ("m.py", "async def f():\n    raise NotImplementedError()\n", "f"),
    ("a.ts", "export function complete(args: string[]): string[] {\n"
             "  throw new Error('Not implemented');\n}\n", "complete"),
    ("a.js", "class A {\n  run(x) {\n    throw new Error(\"TODO\");\n  }\n}\n", "run"),
    ("a.js", "const go = (x) => {\n  throw new Error('not yet implemented')\n}\n", "go"),
    ("A.java", "public class A {\n  public List<String> complete(String s) {\n"
               "    throw new UnsupportedOperationException(\"Not implemented\");\n  }\n}\n",
     "complete"),
    ("A.java", "class A {\n  int f() {\n    throw new UnsupportedOperationException();\n  }\n}\n", "f"),
    ("A.kt", "fun complete(s: String): List<String> = TODO(\"later\")\n", "complete"),
    ("A.cs", "public class A {\n  public int F() {\n"
             "    throw new NotImplementedException();\n  }\n}\n", "F"),
    ("a.go", "func (s *S) Complete(a []string) []string {\n"
             "\tpanic(\"not implemented\")\n}\n", "Complete"),
    ("a.rs", "pub fn complete(a: &[String]) -> Vec<String> {\n    todo!()\n}\n", "complete"),
    ("a.rs", "fn f() -> u8 {\n    unimplemented!(\"x\")\n}\n", "f"),
    ("a.rb", "class A\n  def complete(args)\n    raise NotImplementedError\n  end\nend\n",
     "complete"),
    ("a.php", "<?php\nfunction complete($a) {\n"
              "    throw new \\LogicException('Not implemented');\n}\n", "complete"),
    ("a.swift", "func complete() -> [String] {\n    fatalError(\"not implemented\")\n}\n",
     "complete"),
    ("a.scala", "def complete(a: Seq[String]): Seq[String] = ???\n", "complete"),
    ("a.cpp", "std::vector<int> complete(int a) {\n"
              "  throw std::logic_error(\"not implemented\");\n}\n", "complete"),
])
def test_stub_found(path, text, name):
    found = stubs.find_stubs(path, text)
    assert [n for n, _, _ in found] == [name], found


@pytest.mark.parametrize("path,text", [
    # real bodies
    ("m.py", "def f(x):\n    return x + 1\n"),
    ("m.py", "def f(x):\n    if not x:\n        raise NotImplementedError\n    return x\n"),
    ("m.py", "def f():\n    pass\n"),                       # a no-op hook is legit
    # declarations that are meant to have no body
    ("m.py", "from abc import ABC, abstractmethod\nclass B(ABC):\n"
             "    @abstractmethod\n    def f(self):\n        raise NotImplementedError\n"),
    ("m.py", "from typing import Protocol\nclass P(Protocol):\n    def f(self) -> int: ...\n"),
    ("m.py", "from typing import overload\n@overload\ndef f(x: int) -> int: ...\n"
             "@overload\ndef f(x: str) -> str: ...\ndef f(x):\n    return x\n"),
    ("m.pyi", "def f(x: int) -> int: ...\n"),
    ("m.py", "class Shell:\n    def source(self):\n        raise NotImplementedError\n"
             "class Bash(Shell):\n    def source(self):\n        return 'x'\n"),
    ("a.ts", "export abstract class A {\n  abstract run(): void;\n}\n"),
    ("a.ts", "function f(x) {\n  if (!x) {\n    throw new Error('x missing');\n  }\n"
             "  return x;\n}\n"),
    ("a.ts", "function f(x) {\n  const y = g(x);\n  throw new Error('not implemented');\n}\n"),
    ("A.java", "interface I {\n  default int f() {\n"
               "    throw new UnsupportedOperationException(\"not implemented\");\n  }\n}\n"),
    ("A.java", "class A {\n  void f() {\n    // throw new UnsupportedOperationException();\n"
               "    run();\n  }\n}\n"),
    ("a.go", "func f() error {\n\treturn errors.New(\"bad input\")\n}\n"),
    ("a.rs", "fn f() -> u8 {\n    1\n}\n"),
    ("a.rb", "def f\n  42\nend\n"),
    ("README.md", "raise NotImplementedError"),
])
def test_not_a_stub(path, text):
    assert not stubs.find_stubs(path, text)


def test_only_new_stubs_count():
    before = "def hook():\n    raise NotImplementedError\n"
    now = before + "def core():\n    raise NotImplementedError\n"
    assert [n for n, _, _ in stubs.new_stubs("m.py", before, now)] == ["core"]
    assert stubs.new_stubs("m.py", before, before) == []
    assert [n for n, _, _ in stubs.new_stubs("m.py", None, now)] == ["hook", "core"]


def test_syntax_error_is_unknown():
    assert stubs.find_stubs("m.py", "def f(:\n") is None


# ── chat side ────────────────────────────────────────────────────────────────
def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def hook():\n    raise NotImplementedError\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "seed"], check=True)
    return subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def test_note_once_and_facts(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _notes, _stub_note
    head = _repo(tmp_path)
    held = []
    monkeypatch.setattr(_notes, "hold", lambda st, text: held.append(text))
    st = types.SimpleNamespace(cwd=str(tmp_path), head0=head)
    core = tmp_path / "pkg" / "core.py"
    core.write_text("def hook():\n    raise NotImplementedError\n\n"
                    "def get_completions(args):\n    raise NotImplementedError\n")
    ev = list(_stub_note.check(st, [("pkg/core.py", str(core))], str(tmp_path)))
    assert len(ev) == 1 and "get_completions" in ev[0]["text"]
    assert "`hook`" not in held[0]
    # same stubs again: said once
    assert list(_stub_note.check(st, [("pkg/core.py", str(core))], str(tmp_path))) == []
    assert "get_completions" in _stub_note.stubs_left(str(tmp_path), head)
    # implemented: the facts line clears
    core.write_text("def hook():\n    raise NotImplementedError\n\n"
                    "def get_completions(args):\n    return list(args)\n")
    assert list(_stub_note.check(st, [("pkg/core.py", str(core))], str(tmp_path))) == []
    assert _stub_note.stubs_left(str(tmp_path), head) == ""


def test_test_files_and_switch_ignored(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _stub_note
    head = _repo(tmp_path)
    st = types.SimpleNamespace(cwd=str(tmp_path), head0=head)
    (tmp_path / "tests").mkdir()
    fake = tmp_path / "tests" / "test_x.py"
    fake.write_text("class Fake:\n    def run(self):\n        raise NotImplementedError\n")
    assert list(_stub_note.check(st, [("tests/test_x.py", str(fake))], str(tmp_path))) == []
    core = tmp_path / "pkg" / "core.py"
    core.write_text("def new():\n    ...\n")
    monkeypatch.setenv("AIFORGE_CHAT_STUB_NOTE", "0")
    assert list(_stub_note.check(st, [("pkg/core.py", str(core))], str(tmp_path))) == []


@pytest.mark.parametrize("path,word", [
    ("m.py", "abstractmethod"), ("a.ts", "abstract"), ("A.java", "interface"),
    ("A.kt", "TODO()"), ("A.cs", "NotImplementedException"), ("a.go", "panics"),
    ("a.rs", "trait"), ("a.rb", "NotImplementedError"), ("a.php", "abstract"),
    ("a.swift", "protocol"), ("a.scala", "???"), ("a.cpp", "virtual"),
])
def test_language_hint(path, word):
    assert word in stubs.hint(path)
    assert "test" not in stubs.hint(path).lower()


def test_no_hint_for_unknown_language():
    assert stubs.hint("notes.txt") == ""


def test_note_carries_language_hint(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _notes, _stub_note
    head = _repo(tmp_path)
    held = []
    monkeypatch.setattr(_notes, "hold", lambda st, text: held.append(text))
    st = types.SimpleNamespace(cwd=str(tmp_path), head0=head)
    core = tmp_path / "pkg" / "core.py"
    core.write_text("def core():\n    raise NotImplementedError\n")
    list(_stub_note.check(st, [("pkg/core.py", str(core))], str(tmp_path)))
    assert "abstractmethod" in held[0] and "runtime" in held[0]
