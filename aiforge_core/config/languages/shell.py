"""Shell (bash) language profile."""
from __future__ import annotations

from aiforge_core.config.languages.base import LanguageProfile

# Commands copied verbatim from repo_standards._DEFAULTS_BY_LANG["shell"].
# syntax_check mirrors syntax_guard._CHECKERS[".sh"] / [".bash"] (bash -n).
PROFILE = LanguageProfile(
    name="shell",
    aliases=("bash", "sh"),
    extensions=(".sh", ".bash"),
    build_markers=(),
    compile_cmd="bash -n",
    test_cmd="bats .",
    lint_cmd="shellcheck",
    format_cmd="shfmt -w .",
    syntax_check=("bash", lambda b, f: [b, "-n", f]),
    toolchain_candidates={
        "bash": ("bash", "sh"),
        "shellcheck": ("shellcheck",),
        "shfmt": ("shfmt",),
    },
    install_hint="Install bash; shellcheck (lint) and shfmt (format) optional.",
    conventions=(
        "`set -euo pipefail`; quote all expansions (\"$var\"); prefer [[ ]] over "
        "[ ]; lint with shellcheck; test with bats; syntax-check with `bash -n`."
    ),
)

__all__ = ["PROFILE"]
