"""read_lines must honour the range forms models actually send.

Bench logs (11-11): 69 of 70 reads sent ``{"range": [2179, 2350]}`` and each
came back as the whole file from line 1 (capped), so the model never saw the
lines it asked for and asked again.
"""
import pytest

from aiforge_core.runtime.chat_agent._tools._code import _t_read_lines


@pytest.fixture
def big(tmp_path):
    (tmp_path / "f.py").write_text("".join(f"line{i}\n" for i in range(1, 3001)))
    return str(tmp_path)


def _first_line(out):
    text = out["text"] if "text" in out else out["parts"][0]["text"]
    return text.splitlines()[0]


@pytest.mark.parametrize("args", [
    {"range": [2000, 2100]},
    {"range": "2000-2100"},
    {"range": "2000,2100"},
    {"range": "2000:2100"},
    {"range": [[2000, 2100]]},
    {"offset": 2000, "offset_end": 2100},
    {"lines": [2000, 2100]},
    {"line_start": 2000, "line_end": 2100},
    {"start_line": 2000, "end_line": 2100},
])
def test_range_forms_start_at_the_asked_line(big, args):
    out = _t_read_lines({"path": "f.py", **args}, big)
    assert out["ok"]
    assert int(_first_line(out)[4:]) <= 2000 <= out.get("end", 10**9)
    assert int(_first_line(out)[4:]) > 1500  # not the file head


def test_single_line_number_reads_around_it(big):
    out = _t_read_lines({"path": "f.py", "range": 2500}, big)
    assert out["ok"]
    assert out["start"] <= 2500 <= out["end"]
    assert out["start"] > 1500


def test_string_ranges_entries(big):
    out = _t_read_lines({"path": "f.py", "ranges": ["100-110", "2000:2010"]}, big)
    assert [p["start"] for p in out["parts"]] == [100, 2000]


def test_unknown_argument_is_an_error_not_the_whole_file(big):
    out = _t_read_lines({"path": "f.py", "from_line": 2000}, big)
    assert out["ok"] is False
    assert "from_line" in out["error"] and "start" in out["error"]


def test_bad_range_value_is_an_error(big):
    out = _t_read_lines({"path": "f.py", "range": "middle"}, big)
    assert out["ok"] is False and "range" in out["error"]


def test_path_only_still_reads_from_the_top(big):
    out = _t_read_lines({"path": "f.py"}, big)
    assert out["ok"] and out["start"] == 1


def test_several_spans_under_range_read_as_ranges(big):
    out = _t_read_lines({"path": "f.py", "range": [[100, 110], [2000, 2010]]}, big)
    assert [p["start"] for p in out["parts"]] == [100, 2000]


def test_full_flag_is_accepted(big):
    out = _t_read_lines({"path": "f.py", "full": True}, big)
    assert out["ok"] and out["start"] == 1
