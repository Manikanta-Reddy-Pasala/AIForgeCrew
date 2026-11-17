"""Failures the original code has too, for non-pytest runners; reads past the end."""
import os
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _base_reds as br
from aiforge_core.runtime.chat_agent._tools._code import _t_read_lines

CARGO_RED = """running 3 tests
test test_basic ... ok
test test_parse_errors ... FAILED
test test_other ... ok

failures:
    test_parse_errors

test result: FAILED. 2 passed; 1 failed; 0 ignored
"""


def test_failing_names_of_several_runners():
    assert br.failing_names(CARGO_RED) == {"test_parse_errors"}
    assert br.failing_names("--- FAIL: TestParse (0.00s)\nFAIL") == {"TestParse"}
    mvn = "[ERROR] Failures: \n[ERROR]   OrderServiceTest.totals:42 expected: <3> but was: <2>\n"
    assert br.failing_names(mvn) == {"OrderServiceTest.totals"}
    assert br.failing_names("OrderTest > totals() FAILED\n") == {"OrderTest > totals()"}
    assert br.failing_names("  ✕ adds numbers (3 ms)\n") == {"adds numbers"}
    assert br.failing_names("FAILED tests/test_a.py::test_x - assert 1\n") == {"tests/test_a.py::test_x"}


def _repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (r / "a.txt").write_text("x\n")
    g("add", ".")
    g("commit", "-qm", "base")
    rev = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    return r, rev


def test_same_reds_at_base_vouches_only_for_old_failures(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_BASE_REDS", raising=False)
    br._CACHE.clear()
    r, rev = _repo(tmp_path)
    st = SimpleNamespace(cwd=str(r), head0=rev)
    # the base prints the same cargo failure: an old failure
    cmd = f"cd {r} && printf '%s' \"$(cat <<'X'\n{CARGO_RED}X\n)\""
    assert br.same_reds_at_base(st, str(r), cmd, CARGO_RED)
    # a failure the base does not have is the turn's own
    br._CACHE.clear()
    new_red = CARGO_RED.replace("test_parse_errors", "test_new_bug")
    assert not br.same_reds_at_base(st, str(r), cmd, new_red)
    # cannot name every failure: no verdict
    br._CACHE.clear()
    assert not br.same_reds_at_base(st, str(r), cmd, "error: 3 tests failed\n")
    monkeypatch.setenv("AIFORGE_CHAT_BASE_REDS", "0")
    br._CACHE.clear()
    assert not br.same_reds_at_base(st, str(r), cmd, CARGO_RED)


def test_read_past_the_end_shows_the_end(tmp_path):
    f = tmp_path / "t.rs"
    f.write_text("".join(f"line {i}\n" for i in range(1, 427)))
    r = _t_read_lines({"path": str(f), "offset": 700}, str(tmp_path))
    assert r["total_lines"] == 426 and r["end"] == 426 and "line 426" in r["text"]
    assert "only 426 lines" in r["note"]
