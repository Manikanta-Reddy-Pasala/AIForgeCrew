"""read_lines with many ranges stops at a char cap, on whole lines, and says so."""
from aiforge_core.runtime.chat_agent._tools import _code


def test_whole_file_by_ranges_is_capped_with_a_note(tmp_path):
    f = tmp_path / "Big.java"
    f.write_text("".join(f"    int field{i} = {i}; // padding padding padding\n" for i in range(1, 7001)))
    ranges = [[s, s + 199] for s in range(1, 7000, 200)]
    r = _code._t_read_lines({"path": str(f), "ranges": ranges}, str(tmp_path))
    text = "".join(p["text"] for p in r["parts"])
    assert len(text) <= _code._RANGES_CHARS and text.endswith("\n")
    assert "stopped at line" in r["note"] and "7000 lines" in r["note"]
    last = r["parts"][-1]
    assert text.count("\n") == sum(p["end"] - p["start"] + 1 for p in r["parts"])
    assert r["note"].startswith(f"stopped at line {last['end'] + 1}")


def test_small_ranges_read_in_full_without_note(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("".join(f"x{i} = {i}\n" for i in range(1, 301)))
    r = _code._t_read_lines({"path": str(f), "ranges": [[1, 10], [100, 120]]}, str(tmp_path))
    assert [(p["start"], p["end"]) for p in r["parts"]] == [(1, 10), (100, 120)]
    assert "note" not in r
