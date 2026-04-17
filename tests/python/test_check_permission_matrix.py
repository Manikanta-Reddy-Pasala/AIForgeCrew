# tests/python/test_check_permission_matrix.py
import subprocess
import sys


def test_matrix_matches_design():
    result = subprocess.run(
        [sys.executable, "tools/check_permission_matrix.py"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
