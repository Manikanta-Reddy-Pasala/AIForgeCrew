"""Tests that could not start for a missing dependency: said at once, with the
interpreter that ran them (tomlkit: yaml missing, an hour with no test result)."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _env_guard as E

COLLECT_FAIL = """
==================================== ERRORS ====================================
______________________ ERROR collecting tests/test_toml_tests.py _______________________
ImportError while importing test module '/r/tests/test_toml_tests.py'.
E   ModuleNotFoundError: No module named 'yaml'
=========================== short test summary info ============================
ERROR tests/test_toml_tests.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
"""


def _repo(tmp_path):
    (tmp_path / "tomlkit").mkdir()
    (tmp_path / "tomlkit" / "__init__.py").write_text("")
    return str(tmp_path)


def test_a_missing_third_party_module_at_collection_is_named(tmp_path):
    top = _repo(tmp_path)
    assert E.missing_modules(COLLECT_FAIL, top) == ["yaml"]
    st = SimpleNamespace()
    text = E.note(st, "run_command", {"cmd": "/usr/bin/python3 -m pytest tests -q"}, COLLECT_FAIL, top)
    assert "`yaml` is not installed" in text and "/usr/bin/python3 -m pip install pyyaml" in text
    assert ".venv/bin/python -m pytest" in text
    assert E.note(st, "run_command", {"cmd": "python3 -m pytest"}, COLLECT_FAIL, top) == ""  # once


def test_the_repos_own_module_or_an_ordinary_failure_is_not_this(tmp_path):
    top = _repo(tmp_path)
    own = COLLECT_FAIL.replace("'yaml'", "'tomlkit.parser2'")
    assert E.missing_modules(own, top) == []          # the code under change: a real error
    assert E.missing_modules("E   ModuleNotFoundError: No module named 'yaml'\n1 failed", top) == []
    assert E.missing_modules("3 failed, 10 passed", top) == []


def test_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_ENV_GUARD", "0")
    assert E.note(SimpleNamespace(), "run_command", {"cmd": "python3 -m pytest"},
                  COLLECT_FAIL, _repo(tmp_path)) == ""
