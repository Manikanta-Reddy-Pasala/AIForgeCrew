"""Regression: test discovery must find tests under a workspace whose PATH
contains '.aiforge' (chat sessions live at ~/.aiforge/chat-workspaces/…), while
still skipping real artifact dirs (.aiforge-venv, __pycache__)."""
import os


def test_finds_tests_when_workspace_path_contains_aiforge(tmp_path):
    from aiforge_core.runtime.integration_report import _python_test_files
    # Mimic ~/.aiforge/chat-workspaces/session-N — the '.aiforge' segment is in
    # the ABSOLUTE path, not an artifact inside the tree.
    ws = tmp_path / ".aiforge" / "chat-workspaces" / "session-9"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_api.py").write_text("def test_ok():\n    assert True\n")
    (ws / "app_test.py").write_text("def test_two():\n    assert True\n")
    found = _python_test_files(str(ws))
    names = {os.path.basename(f) for f in found}
    assert "test_api.py" in names, found
    assert "app_test.py" in names, found


def test_skips_artifact_dirs(tmp_path):
    from aiforge_core.runtime.integration_report import _python_test_files
    ws = tmp_path / ".aiforge" / "session-1"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_real.py").write_text("def test_ok():\n    assert True\n")
    # A stray test file inside the managed venv must NOT be collected.
    venv = ws / ".aiforge-venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "test_vendored.py").write_text("def test_bad():\n    assert False\n")
    pyc = ws / "__pycache__"
    pyc.mkdir()
    (pyc / "test_cached.py").write_text("x = 1\n")
    found = _python_test_files(str(ws))
    names = {os.path.basename(f) for f in found}
    assert "test_real.py" in names, found
    assert "test_vendored.py" not in names, found
    assert "test_cached.py" not in names, found
