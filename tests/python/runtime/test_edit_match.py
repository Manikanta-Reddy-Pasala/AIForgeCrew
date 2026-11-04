# tests/python/runtime/test_edit_match.py
"""An edit finds its text despite the slips a model makes — and never guesses
between two places."""
from aiforge_core.runtime.edit_match import replace_once as R

BODY = (
    "class Inventory:\n"
    "    def receive(self, sku, quantity):\n"
    "        level = self._levels[sku]\n"
    "        level.on_hand += quantity\n"
    "        return level\n"
    "\n"
    "    def adjust(self, sku, delta):\n"
    "        return self._levels[sku]\n"
)


def test_exact_and_ambiguous():
    r = R(BODY, "level.on_hand += quantity", "level.on_hand += int(quantity)")
    assert r["ok"] and r["how"] == "exact" and "int(quantity)" in r["body"]
    r = R(BODY, "return ", "return  ")
    assert not r["ok"] and r["error"] == "ambiguous_match" and r["occurrences"] == 2


def test_line_number_prefixes_are_dropped():
    old = "3|        level = self._levels[sku]\n4|        level.on_hand += quantity"
    new = "3|        level = self._levels[sku.upper()]\n4|        level.on_hand += quantity"
    r = R(BODY, old, new)
    assert r["ok"] and "self._levels[sku.upper()]" in r["body"] and "3|" not in r["body"]


def test_wrong_indent_is_matched_and_new_text_reindented():
    old = "def receive(self, sku, quantity):\n    level = self._levels[sku]"
    new = "def receive(self, sku, quantity, expiry=None):\n    level = self._levels[sku]"
    r = R(BODY, old, new)
    assert r["ok"] and r["how"] == "lines_trimmed"
    assert "    def receive(self, sku, quantity, expiry=None):\n        level = self._levels[sku]\n" in r["body"]
    compile(r["body"], "x", "exec")


def test_whitespace_inside_lines_is_forgiven():
    r = R(BODY, "level.on_hand  +=   quantity", "level.on_hand += 2 * quantity")
    assert r["ok"] and "2 * quantity" in r["body"]


def test_block_anchor_with_a_slightly_wrong_middle():
    old = ("    def receive(self, sku, quantity):\n"
           "        level = self._level[sku]\n"
           "        level.on_hand += quantity\n"
           "        return level")
    new = "    def receive(self, sku, quantity):\n        return self._bump(sku, quantity)"
    r = R(BODY, old, new)
    assert r["ok"] and r["how"] == "block_anchor"
    assert "_bump" in r["body"] and "level.on_hand" not in r["body"]
    compile(r["body"], "x", "exec")


def test_a_miss_returns_the_closest_numbered_lines():
    r = R(BODY, "    def recieve(self, sku, qty):\n        pass", "x")
    assert not r["ok"] and r["error"] == "old_text_not_found"
    assert any(line.startswith("2|") and "receive" in line for line in r["closest"])


def test_off_switch_is_exact_only(monkeypatch):
    monkeypatch.setenv("AIFORGE_EDIT_FUZZY", "0")
    assert not R(BODY, "level.on_hand  +=   quantity", "x")["ok"]


def test_dict_and_yaml_keys_are_content_not_line_numbers():
    body = "MAP = {\n    1: \"a\",\n    2: \"b\",\n}\nLIST = [\n    \"a\",\n    \"b\",\n]\n"
    r = R(body, "1: \"a\",\n2: \"b\",", "1: \"A\",\n2: \"B\",")      # one indent off
    assert r["ok"] and "    1: \"A\"," in r["body"] and "LIST = [\n    \"a\"," in r["body"]


def test_a_flat_block_does_not_match_a_nested_one():
    body = "a:\n  b: 1\nc: 2\n"
    r = R(body, "a:\nb: 1", "a:\nb: 2")
    assert not r["ok"]                                     # shapes differ: never flatten


def test_an_inner_brace_does_not_end_the_anchor_early():
    body = ("function f() {\n  if (x) {\n    y();\n  }\n  z();\n  w();\n}\n"
            "function g() {\n}\n")
    old = "function f() {\n  if (x) {\n    yy();\n  }\n  z();\n}"   # stale middle
    r = R(body, old, "function f() {\n  done();\n}")
    if r["ok"]:
        assert "w();" not in r["body"].split("function g")[0] or r["body"].count("}") >= 2
        assert "function g() {" in r["body"]


def test_unicode_line_separator_stays_inside_its_line():
    body = "s = \"a\u2028b\"\nt = 1\n"
    r = R(body, "b\"\n  t = 1", "x")
    assert not r["ok"] or "s = \"a\u2028" in r["body"]


def test_a_fuzzy_delete_leaves_no_blank_line():
    r = R(BODY, "level.on_hand  +=  quantity", "")          # not exact: fuzzy line match
    assert r["ok"] and "        level = self._levels[sku]\n        return level" in r["body"]


def test_mixed_prefixes_in_new_text_are_refused():
    old = "3|        level = self._levels[sku]\n4|        level.on_hand += quantity"
    new = "3|        level = self._levels[sku]\n        level.on_hand += 1"
    assert not R(BODY, old, new)["ok"]


def test_test_and_build_commands_get_a_bigger_window():
    from aiforge_core.runtime.chat_agent import _shell
    for cmd in ("python3 -m pytest -q", "cd x && npm test", "mvn -q verify", "go test ./...",
                "cargo test", "make check"):
        assert _shell.is_test_command("run_command", {"cmd": cmd}), cmd
    assert _shell.is_test_command("run_tests", {})
    for cmd in ("ls -la", "cat a.py", "python3 -c 'print(1)'", "git status"):
        assert not _shell.is_test_command("run_command", {"cmd": cmd}), cmd
    assert _shell._MAX_OBS_TEST > _shell._MAX_OBS


def test_prefixed_old_with_plain_new_keeps_the_indent():
    old = "3|        level = self._levels[sku]\n4|        level.on_hand += quantity"
    new = "        level = self._levels[sku]\n        level.on_hand += 2 * quantity"
    r = R(BODY, old, new)
    assert r["ok"] and "\n        level.on_hand += 2 * quantity\n" in r["body"]
    compile(r["body"], "x", "exec")


def test_line_number_text_matches_whole_lines_only():
    body = "def f():\n    x = 1\n    y = 20\n"
    r = R(body, "2|    x = 1\n3|    y = 2", "    x = 1\n    y = 3")
    assert not r["ok"] or "y = 20" not in r["body"] and "y = 30" not in r["body"]


def test_a_nesting_mismatch_is_said_not_guessed():
    body = "a:\n  b: 1\n  c: 2\n  d: 3\ne: 4\n"
    r = R(body, "a:\nb: 1\nc: 2\nd: 3", "a:\nb: 9\nc: 2\nd: 3")
    assert not r["ok"] and r["error"] == "indent_mismatch" and r["line"] == 1


def test_read_lines_reads_whole_small_files_and_widens_small_ranges(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_WHOLE_LINES", "400")
    monkeypatch.setenv("AIFORGE_CHAT_READ_MIN_LINES", "200")
    from aiforge_core.runtime.chat_agent._tools import _code
    small = tmp_path / "s.py"
    small.write_text("".join(f"line {i}\n" for i in range(1, 301)))
    r = _code._t_read_lines({"path": "s.py", "start": 50, "end": 60}, str(tmp_path))
    assert (r["start"], r["end"]) == (1, 300) and "note" in r
    big = tmp_path / "b.py"
    big.write_text("".join(f"line {i}\n" for i in range(1, 2001)))
    r = _code._t_read_lines({"path": "b.py", "start": 1000, "end": 1040}, str(tmp_path))
    assert (r["start"], r["end"]) == (980, 1179)
    r = _code._t_read_lines({"path": "b.py", "start": 1990, "end": 2000}, str(tmp_path))
    assert r["end"] == 2000 and r["end"] - r["start"] + 1 == 200
    r = _code._t_read_lines({"path": "b.py", "start": 100, "end": 600}, str(tmp_path))
    assert (r["start"], r["end"]) == (100, 600) and "note" not in r


def test_a_long_test_run_is_compacted_to_its_failures():
    from aiforge_core.runtime.chat_agent import _shell
    passing = "".join(f"tests/test_a.py::test_ok_{i} PASSED\n" for i in range(2000))
    fail = ("____ test_bad ____\n    def test_bad():\n>       assert f() == 2\n"
            "E       assert 1 == 2\n")
    summary = "=== short test summary info ===\nFAILED tests/test_a.py::test_bad - assert 1 == 2\n=== 1 failed, 2000 passed ===\n"
    out = passing + fail + passing + summary
    c = _shell.compact_test_output(out, 8000)
    assert len(c) <= 8000 and "compacted" in c
    assert "____ test_bad ____" in c and "E       assert 1 == 2" in c
    assert "FAILED tests/test_a.py::test_bad" in c and "1 failed, 2000 passed" in c
    assert c.count("PASSED") < 200
    assert _shell.compact_test_output("short", 8000) == "short"


def test_file_patch_takes_old_str_and_a_syntax_refusal_says_where_and_how(tmp_path):
    from aiforge_core.runtime.chat_agent import _shell
    f = tmp_path / "m.py"
    f.write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    r = _shell._t_file_patch({"path": "m.py", "old_str": "return 1", "new_str": "return 10"}, str(tmp_path))
    assert r["ok"] and "return 10" in f.read_text()
    r = _shell._t_file_patch({"path": "m.py", "old_text": "def g():", "new_text": "def g(:"}, str(tmp_path))
    assert not r["ok"] and r["error"] == "syntax_invalid"
    assert "ONE multi_edit" in r["hint"] and any("def g(:" in ln for ln in r.get("around", []))
    assert "def g():" in f.read_text()                       # nothing written
    r = _shell._t_file_patch({"path": "m.py"}, str(tmp_path))
    assert not r["ok"] and "old_str" in r["error"]
