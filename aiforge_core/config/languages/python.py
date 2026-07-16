"""Python language profile."""
from __future__ import annotations

from aiforge_core.config.languages.base import LanguageProfile

# Commands copied verbatim from repo_standards._DEFAULTS_BY_LANG["python"].
# syntax_check is None: syntax_guard validates .py in-process via compile(),
# so Python is deliberately absent from its external _CHECKERS table.
PROFILE = LanguageProfile(
    name="python",
    aliases=("py", "python3"),
    extensions=(".py", ".pyi"),
    build_markers=("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
    compile_cmd="python -m compileall -q .",
    test_cmd="python -m pytest -q",
    lint_cmd="ruff check .",
    format_cmd="ruff format .",
    syntax_check=None,
    toolchain_candidates={"python": ("python3", "python")},
    install_hint="Install Python 3 (python3) and `uv` (or pip) for deps.",
    conventions=(
        "PEP 8; type hints on public APIs; test with pytest (test_*.py / "
        "*_test.py); prefer pathlib over os.path; f-strings; avoid bare "
        "except — catch specific exceptions."
    ),
)

__all__ = ["PROFILE"]
