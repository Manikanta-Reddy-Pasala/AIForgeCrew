"""A same-failure trip after source edits says the fix may be in the wrong place."""
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _escalate as es


def _repo(tmp_path):
    r = tmp_path / "r"
    (r / "src").mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "src" / "Tax.java").write_text("class Tax {}\n")
    g("add", "."); g("commit", "-qm", "base")
    rev = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    return r, rev


def test_hint_names_the_edited_sources_once(tmp_path):
    r, rev = _repo(tmp_path)
    (r / "src" / "Tax.java").write_text("class Tax { int x; }\n")
    (r / "src" / "TaxTest.java").write_text("class TaxTest {}\n")
    st = SimpleNamespace(cwd=str(r), head0=rev)
    h = es._wrong_place_hint(st, "I keep hitting the same failure after several different fixes")
    assert "src/Tax.java" in h and "TaxTest" not in h and "Trace it back" in h
    assert es._wrong_place_hint(st, "same failure again") == ""            # once


def test_no_hint_for_other_trips_or_no_edits(tmp_path):
    r, rev = _repo(tmp_path)
    st = SimpleNamespace(cwd=str(r), head0=rev)
    assert es._wrong_place_hint(st, "same failure") == ""                  # nothing edited
    (r / "src" / "Tax.java").write_text("class Tax { int x; }\n")
    assert es._wrong_place_hint(st, "repeated read_lines") == ""           # not a same-failure trip
