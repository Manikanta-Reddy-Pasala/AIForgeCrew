"""Edits keep a CRLF file CRLF; LF files and new files are unchanged."""
from aiforge_core.runtime import line_endings as le
from aiforge_core.runtime.chat_agent._shell import _t_file_patch, _t_file_write
from aiforge_core.runtime.chat_agent._tools._skills import _t_multi_edit

SRC = "int a = 1;\r\nint b = 2;\r\nint c = 3;\r\n"


def _crlf(tmp_path):
    f = tmp_path / "x.cpp"
    f.write_bytes(SRC.encode())
    return f


def test_multi_edit_keeps_crlf(tmp_path):
    f = _crlf(tmp_path)
    r = _t_multi_edit({"edits": [{"path": "x.cpp", "old_str": "int b = 2;\nint c", "new_str": "int b = 5;\nint c"}]},
                      str(tmp_path))
    assert r["ok"], r
    assert f.read_bytes() == b"int a = 1;\r\nint b = 5;\r\nint c = 3;\r\n"


def test_file_patch_keeps_crlf(tmp_path):
    f = _crlf(tmp_path)
    r = _t_file_patch({"path": "x.cpp", "old_text": "int a = 1;", "new_text": "int a = 9;\nint z = 0;"},
                      str(tmp_path))
    assert r["ok"], r
    assert f.read_bytes() == b"int a = 9;\r\nint z = 0;\r\nint b = 2;\r\nint c = 3;\r\n"


def test_file_write_over_crlf_keeps_it_new_file_lf(tmp_path):
    f = _crlf(tmp_path)
    _t_file_write({"path": "x.cpp", "content": "int q;\nint r;\n"}, str(tmp_path))
    assert f.read_bytes() == b"int q;\r\nint r;\r\n"
    _t_file_write({"path": "n.cpp", "content": "int q;\n"}, str(tmp_path))
    assert (tmp_path / "n.cpp").read_bytes() == b"int q;\n"


def test_lf_file_stays_lf(tmp_path):
    f = tmp_path / "y.py"
    f.write_bytes(b"a = 1\nb = 2\n")
    _t_file_patch({"path": "y.py", "old_text": "a = 1", "new_text": "a = 3"}, str(tmp_path))
    assert f.read_bytes() == b"a = 3\nb = 2\n"
    assert le.style_of(f) == le.LF
