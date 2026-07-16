"""C language profile."""
from __future__ import annotations

from aiforge_core.config.languages.base import LanguageProfile

# Commands copied verbatim from repo_standards._DEFAULTS_BY_LANG["c"].
# syntax_check mirrors syntax_guard._CHECKERS[".c"] (gcc -fsyntax-only). Header
# files (.h) get an extra `-x c` override applied in syntax_guard, so they are
# intentionally NOT distinguished at the profile level.
PROFILE = LanguageProfile(
    name="c",
    aliases=(),
    extensions=(".c", ".h"),
    build_markers=("CMakeLists.txt", "Makefile", "makefile"),
    compile_cmd="make",
    test_cmd="make test",
    lint_cmd="",
    format_cmd="clang-format -i **/*.c **/*.h",
    syntax_check=("gcc", lambda b, f: [b, "-fsyntax-only", f]),
    toolchain_candidates={
        "compiler": ("cc", "gcc", "clang"),
        "make": ("make",),
        "cmake": ("cmake",),
    },
    install_hint="Install a C compiler (gcc/clang) plus make and/or cmake.",
    conventions=(
        "C11+; check every malloc/return; free what you allocate; no VLAs in "
        "hot paths; build with make/cmake; format with clang-format; "
        "syntax-check with `gcc -fsyntax-only`."
    ),
)

__all__ = ["PROFILE"]
