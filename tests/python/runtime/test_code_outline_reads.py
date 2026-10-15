"""Big source files are read as an outline, one symbol at a time, and a line
range read actually shows its lines.

``read_lines`` returned its lines under ``text`` while the renderer printed
only ``content``: since 2026-09-26 the model got the path and nothing else, so
every "truncated — use read_lines for the rest" ended in a guess."""
from __future__ import annotations

import pytest

from aiforge_core.runtime.chat_agent._obs_text import render_observation
from aiforge_core.runtime.chat_agent._shell import _t_file_read, _t_read_files
from aiforge_core.runtime.chat_agent._tools._code import _t_read_lines
from aiforge_core.runtime.tools import code_outline

_PY = '''import os


class Cart:
    """A cart."""

    def __init__(self):
        self.items = []

    def add(self, sku, qty=1):
        self.items.append((sku, qty))
        return len(self.items)


def total(cart):
    return sum(q for _, q in cart.items)


class Other:
    def add(self, x):
        return x
'''


@pytest.fixture
def big_py(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "200")
    path = tmp_path / "cart.py"
    path.write_text(_PY)
    return tmp_path, path


def test_read_lines_shows_the_lines_with_their_numbers(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\nfour\n")
    result = _t_read_lines({"path": "a.txt", "start": 2, "end": 3}, str(tmp_path))
    obs = render_observation("read_lines", result, 80000)
    assert "2|two" in obs and "3|three" in obs
    assert "one" not in obs and "four" not in obs


def test_read_lines_past_the_end_says_so(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    result = _t_read_lines({"path": "a.txt", "start": 9}, str(tmp_path))
    assert "file has 1 lines" in render_observation("read_lines", result, 80000)


def test_outline_lists_classes_and_methods_with_ranges(big_py):
    _, path = big_py
    symbols = code_outline.outline(str(path))
    got = [(s.kind, s.name, s.start, s.end, s.depth) for s in symbols]
    assert ("class", "Cart", 4, 12, 0) in got
    assert ("method", "add", 10, 12, 1) in got
    assert ("function", "total", 15, 16, 0) in got


def test_a_big_source_file_reads_as_its_outline(big_py):
    root, _ = big_py
    result = _t_file_read({"path": "cart.py"}, str(root))
    assert "content" not in result and result["total_lines"] == 21
    obs = render_observation("file_read", result, 80000)
    assert "class Cart:" in obs and "def total(cart):" in obs
    assert "self.items.append" not in obs          # bodies are not sent
    assert "symbol" in obs                         # it says how to read a part


def test_full_true_still_reads_the_whole_file(big_py):
    root, _ = big_py
    result = _t_file_read({"path": "cart.py", "full": True}, str(root))
    assert result["content"] == _PY


def test_a_small_file_reads_whole(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "24000")
    (tmp_path / "s.py").write_text(_PY)
    assert _t_file_read({"path": "s.py"}, str(tmp_path))["content"] == _PY


def test_a_symbol_read_returns_that_part_numbered_from_its_line(big_py):
    root, _ = big_py
    result = _t_file_read({"path": "cart.py", "symbol": "Cart.add"}, str(root))
    obs = render_observation("file_read", result, 80000)
    assert "10|    def add(self, sku, qty=1):" in obs
    assert "12|        return len(self.items)" in obs
    assert "def total" not in obs


def test_a_bare_symbol_name_with_two_matches_names_the_other(big_py):
    root, _ = big_py
    result = _t_file_read({"path": "cart.py", "symbol": "add"}, str(root))
    assert result["start"] == 10 and "20" in result["note"]


def test_an_unknown_symbol_returns_the_outline_to_pick_from(big_py):
    root, _ = big_py
    result = _t_file_read({"path": "cart.py", "symbol": "nope"}, str(root))
    obs = render_observation("file_read", result, 80000)
    assert result["ok"] is False and "class Cart:" in obs


def test_a_file_without_a_grammar_reads_as_text(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "10")
    (tmp_path / "notes.unknownext").write_text("x" * 100)
    assert _t_file_read({"path": "notes.unknownext"}, str(tmp_path))["content"] == "x" * 100


def test_read_files_gives_a_big_file_as_its_outline(big_py):
    root, _ = big_py
    (root / "small.txt").write_text("hello")
    out = _t_read_files({"paths": ["cart.py", "small.txt"]}, str(root))
    text = out["content"]
    assert "outline" in text and "class Cart:" in text and "hello" in text


def test_java_outline(tmp_path):
    src = ("package a;\n\npublic class Shop {\n    private int n;\n\n"
           "    public int count() {\n        return n;\n    }\n}\n")
    (tmp_path / "Shop.java").write_text(src)
    symbols = code_outline.outline(str(tmp_path / "Shop.java"))
    names = {(s.name, s.start, s.end) for s in symbols}
    assert ("Shop", 3, 9) in names and ("count", 6, 8) in names


# ── review round 1 ──────────────────────────────────────────────────────────

def test_a_big_file_with_no_symbols_reads_as_text(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "50")
    src = "static const int data[] = {\n" + "  1, 2, 3,\n" * 20 + "};\n"
    (tmp_path / "table.c").write_text(src)
    assert _t_file_read({"path": "table.c"}, str(tmp_path))["content"] == src


def test_a_c_function_outline_spans_its_body(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "10")
    src = ("int add(int a, int b);\n\n"
           "int add(int a, int b)\n{\n    return a + b;\n}\n")
    (tmp_path / "m.c").write_text(src)
    result = _t_file_read({"path": "m.c", "symbol": "add"}, str(tmp_path))
    assert result["start"] == 3 and result["end"] == 6
    assert "return a + b;" in result["text"]


def test_class_dot_method_never_returns_another_classes_method(big_py):
    root, _ = big_py
    # Other.add exists, Cart.remove does not: no silent wrong pick
    result = _t_file_read({"path": "cart.py", "symbol": "Other.remove"}, str(root))
    assert result["ok"] is False
    result = _t_file_read({"path": "cart.py", "symbol": "Cart.x"}, str(root))
    assert result["ok"] is False


def test_a_form_feed_does_not_shift_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "10")
    src = "# a\x0c page break\n\ndef f():\n    return 1\n"
    (tmp_path / "ff.py").write_text(src)
    result = _t_file_read({"path": "ff.py", "symbol": "f"}, str(tmp_path))
    assert result["start"] == 3 and result["text"].startswith("def f():")
    lines = render_observation("read_lines",
                               _t_read_lines({"path": "ff.py", "start": 3, "end": 3},
                                             str(tmp_path)), 1000)
    assert "3|def f():" in lines


def test_overwriting_a_big_source_file_needs_overwrite(big_py):
    from aiforge_core.runtime.chat_agent._shell import _t_file_write
    root, path = big_py
    refused = _t_file_write({"path": "cart.py", "content": "x = 1\n"}, str(root))
    assert refused["ok"] is False and refused["error"] == "big_file_overwrite"
    # the syntax override does not wave a rewrite-from-outline through
    refused = _t_file_write({"path": "cart.py", "content": "x = (\n", "force": True},
                            str(root))
    assert refused["error"] == "big_file_overwrite" and path.read_text() == _PY
    done = _t_file_write({"path": "cart.py", "content": "x = 1\n", "overwrite": True},
                         str(root))
    assert done["ok"] is True and path.read_text() == "x = 1\n"


def test_a_big_file_read_whole_can_be_overwritten(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._shell import _t_file_write
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "50")
    (tmp_path / "t.c").write_text("static int d[] = {\n" + "1,\n" * 40 + "};\n")
    assert _t_file_write({"path": "t.c", "content": "int x;\n"}, str(tmp_path))["ok"]


def test_go_rust_cpp_methods_found_by_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "10")
    (tmp_path / "s.go").write_text("package a\n\ntype Server struct{}\n\n"
                                   "func (s *Server) Start() int {\n\treturn 1\n}\n")
    (tmp_path / "s.rs").write_text("struct Server;\nimpl Server {\n"
                                   "    fn start(&self) -> i32 {\n        1\n    }\n}\n")
    (tmp_path / "s.cpp").write_text("class Server { void start(); };\n\n"
                                    "void Server::start() {\n  int x = 1;\n}\n")
    for name, sym, line in (("s.go", "Server.Start", 5), ("s.rs", "Server.start", 3),
                            ("s.cpp", "Server::start", 3)):
        result = _t_file_read({"path": name, "symbol": sym}, str(tmp_path))
        assert result.get("ok") and result["start"] == line, (name, result)


def test_read_files_caps_a_long_outline(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "100")
    monkeypatch.setenv("AIFORGE_CHAT_READ_FILES_PER_CAP", "300")
    (tmp_path / "many.py").write_text("".join(f"def f{i}(x):\n    return x\n\n"
                                              for i in range(200)))
    text = _t_read_files({"paths": ["many.py"]}, str(tmp_path))["content"]
    assert "more outline rows" in text and len(text) < 600


# ── review round 3 ──────────────────────────────────────────────────────────

def test_another_owners_method_is_never_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "10")
    (tmp_path / "c.go").write_text("package a\n\nfunc (c *Client) Start() int {\n"
                                   "\treturn 1\n}\n")
    (tmp_path / "c.cpp").write_text("void Client::start() {\n  int x = 1;\n}\n")
    (tmp_path / "c.py").write_text("class Client:\n    def start(self):\n        return 1\n")
    for name, sym in (("c.go", "Server.Start"), ("c.cpp", "Server::start"),
                      ("c.py", "Server.start")):
        assert _t_file_read({"path": name, "symbol": sym}, str(tmp_path))["ok"] is False


def test_a_symbol_read_keeps_its_decorators(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_OUTLINE_CHARS", "10")
    (tmp_path / "d.py").write_text("class A:\n    @property\n    def size(self):\n"
                                   "        return 1\n")
    result = _t_file_read({"path": "d.py", "symbol": "A.size"}, str(tmp_path))
    assert result["start"] == 2 and result["text"].lstrip().startswith("@property")


def test_overwrite_accepts_the_usual_truthy_spellings(big_py):
    from aiforge_core.runtime.chat_agent._shell import _t_file_write
    root, _ = big_py
    for yes in (1, "1", "yes", "true"):
        assert _t_file_write({"path": "cart.py", "content": "x = 1\n",
                              "overwrite": yes}, str(root))["ok"] is True
        (root / "cart.py").write_text(_PY)
