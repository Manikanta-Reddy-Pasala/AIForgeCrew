"""C++ language profile."""
from __future__ import annotations

from aiforge_core.config.languages.base import LanguageProfile

# Commands copied verbatim from repo_standards._DEFAULTS_BY_LANG["cpp"].
# syntax_check mirrors syntax_guard._CHECKERS[".cpp"] (g++ -fsyntax-only).
# Header files (.hpp) get an extra `-x c++` override in syntax_guard, so they
# are intentionally NOT distinguished at the profile level.
PROFILE = LanguageProfile(
    name="cpp",
    aliases=("c++", "cplusplus", "cxx"),
    extensions=(".cpp", ".cc", ".cxx", ".hpp"),
    build_markers=("CMakeLists.txt", "Makefile", "makefile"),
    compile_cmd="cmake --build build",
    test_cmd="ctest --output-on-failure --test-dir build",
    lint_cmd="",
    format_cmd="clang-format -i **/*.cpp **/*.hpp",
    syntax_check=("g++", lambda b, f: [b, "-fsyntax-only", f]),
    toolchain_candidates={
        "compiler": ("c++", "g++", "clang++"),
        "make": ("make",),
        "cmake": ("cmake",),
    },
    install_hint="Install a C++ compiler (g++/clang++) plus cmake and/or make.",
    conventions=(
        "Modern C++ (17/20); RAII + smart pointers, no raw new/delete; prefer "
        "std:: containers; build with cmake/ctest; format with clang-format; "
        "syntax-check with `g++ -fsyntax-only`."
    ),
)

__all__ = ["PROFILE"]
