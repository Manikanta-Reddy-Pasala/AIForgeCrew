"""Pre-index path validation — catch a wrong/empty/relative path up front."""
from __future__ import annotations
import os
import tempfile
from aiforge_core.runtime.memory_ingest import validate_path


def test_good_repo_reports_counts():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "src"))
    open(os.path.join(d, "src", "A.java"), "w").write("class A{}")
    open(os.path.join(d, "README.md"), "w").write("x")
    r = validate_path(d)
    assert r["ok"] is True and r["code_files"] == 1 and r["doc_files"] == 1
    assert r["resolved"].endswith(os.path.basename(d))
    assert "A.java" in " ".join(r["sample"])


def test_empty_dir_flagged():
    r = validate_path(tempfile.mkdtemp())
    assert r["ok"] is False and r["exists"] is True and r["code_files"] == 0
    assert "empty" in r["message"].lower()


def test_missing_path_flagged():
    r = validate_path("/no/such/mars-server")
    assert r["ok"] is False and r["exists"] is False
    assert "does not exist" in r["message"]


def test_empty_input():
    assert validate_path("")["ok"] is False
