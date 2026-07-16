"""Tests for the per-language knowledge subsystem (aiforge_core/config/languages).

Two jobs:
  1. The registry itself loads and answers name/alias/extension/detect queries.
  2. The registry MIRRORS the legacy literals in the three rewired consumers
     (repo_standards, integration_report, syntax_guard) for every already-
     supported language — so the rewire changed no output — and adds Kotlin.
"""
from __future__ import annotations

import os

import pytest

from aiforge_core.config import languages as L

FIRST_CLASS = {"python", "java", "kotlin", "shell", "c", "cpp", "rust"}


# ── 1. registry basics ────────────────────────────────────────────────

def test_all_seven_profiles_load():
    assert set(L.PROFILES) >= FIRST_CLASS
    assert len(L.all_profiles()) == len(L.PROFILES)
    for name, prof in L.PROFILES.items():
        assert prof.name == name


def test_every_profile_has_compile_test_and_conventions():
    for prof in L.all_profiles():
        assert prof.compile_cmd.strip(), f"{prof.name}: empty compile_cmd"
        assert prof.test_cmd.strip(), f"{prof.name}: empty test_cmd"
        assert prof.conventions.strip(), f"{prof.name}: empty conventions"


def test_by_name_by_alias_by_extension():
    assert L.by_name("python").name == "python"
    assert L.by_name("PYTHON").name == "python"      # case-insensitive
    assert L.by_name("nope") is None
    # aliases resolve
    assert L.by_alias("c++").name == "cpp"
    assert L.by_alias("kt").name == "kotlin"
    assert L.by_alias("java-gradle").name == "java"
    assert L.by_alias("unknown") is None
    # extensions (leading dot optional)
    assert L.by_extension(".kt").name == "kotlin"
    assert L.by_extension("rs").name == "rust"
    assert L.by_extension(".hpp").name == "cpp"
    assert L.by_extension(".sh").name == "shell"
    assert L.by_extension(".xyz") is None


# ── 2. detect() parity with integration_report._detect_lang ────────────

# integration_report uses granular / lumped labels; map them to the registry's
# canonical profile names for comparison.
_IR_TO_PROFILE = {
    "java-maven": "java", "java-gradle": "java", "c/c++": "cpp",
    "python": "python", "rust": "rust", "shell": "shell", "kotlin": "kotlin",
    "go": "go", "node": "node", "php": "php", "ruby": "ruby",
}


def _write(base, rel, content="x"):
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


@pytest.mark.parametrize("files,expected", [
    ({"pyproject.toml": "", "src/app.py": ""}, "python"),        # marker
    ({"pom.xml": "", "src/Main.java": ""}, "java"),              # maven marker
    ({"build.gradle": "", "src/Main.java": ""}, "java"),         # gradle marker
    ({"Cargo.toml": "", "src/lib.rs": ""}, "rust"),              # marker
    ({"CMakeLists.txt": "", "src/main.cpp": ""}, "cpp"),         # marker
    ({"main.cpp": ""}, "cpp"),                                   # ext fallback
    ({"a.c": ""}, "cpp"),                                        # c/c++ lumped
    ({"run.sh": ""}, "shell"),                                   # ext fallback
    ({"main.py": ""}, "python"),                                 # ext fallback
])
def test_detect_matches_integration_report(tmp_path, files, expected):
    from aiforge_core.runtime import integration_report as ir
    base = str(tmp_path)
    for rel, content in files.items():
        _write(base, rel, content)
    reg = L.detect(base)
    ir_raw = ir._detect_lang(base)
    assert reg == expected
    assert _IR_TO_PROFILE.get(ir_raw) == reg, (
        f"registry={reg} vs integration_report={ir_raw} for {files}")


def test_kotlin_detects_from_kt_and_build_gradle_kts(tmp_path):
    base = str(tmp_path)
    _write(base, "build.gradle.kts", "")
    _write(base, "src/Main.kt", "fun main() {}")
    assert L.detect(base) == "kotlin"


def test_detect_kotlin_from_bare_kt_tree(tmp_path):
    base = str(tmp_path)
    _write(base, "src/App.kts", "println(1)")
    assert L.detect(base) == "kotlin"


def test_detect_none_for_empty_tree(tmp_path):
    assert L.detect(str(tmp_path)) is None


# ── 3. registry MIRRORS repo_standards._DEFAULTS_BY_LANG ────────────────

def test_repo_standards_defaults_mirror_registry():
    from aiforge_core.config import repo_standards as rs
    for name in FIRST_CLASS:
        prof = L.PROFILES[name]
        d = rs._DEFAULTS_BY_LANG[name]
        assert d["compile_cmd"] == prof.compile_cmd, name
        assert d["test_cmd"] == prof.test_cmd, name
        assert d.get("lint_cmd", "") == prof.lint_cmd, name
        assert d["format_cmd"] == prof.format_cmd, name


def test_repo_standards_has_kotlin_default():
    from aiforge_core.config import repo_standards as rs
    assert "kotlin" in rs._DEFAULTS_BY_LANG
    assert rs._DEFAULTS_BY_LANG["kotlin"]["compile_cmd"]
    assert rs._DEFAULTS_BY_LANG["kotlin"]["test_cmd"]


# ── 4. registry MIRRORS syntax_guard._CHECKERS (byte-identical argv) ────

def test_syntax_checkers_argv_unchanged():
    """The registry-built _CHECKERS must produce the exact legacy argv per ext."""
    from aiforge_core.runtime import syntax_guard as sg
    fp = os.path.join(os.sep, "tmp", "synchk", "File.java")  # for javac dirname
    expected = {
        ".sh":   ["bash", "-n", fp],
        ".bash": ["bash", "-n", fp],
        ".c":    ["gcc", "-fsyntax-only", fp],
        ".h":    ["gcc", "-fsyntax-only", "-x", "c", fp],
        ".cpp":  ["g++", "-fsyntax-only", fp],
        ".cc":   ["g++", "-fsyntax-only", fp],
        ".cxx":  ["g++", "-fsyntax-only", fp],
        ".hpp":  ["g++", "-fsyntax-only", "-x", "c++", fp],
        ".java": ["javac", "-d", os.path.dirname(fp), fp],
        ".go":   ["gofmt", "-e", fp],
        ".js":   ["node", "--check", fp],
        ".mjs":  ["node", "--check", fp],
        ".rb":   ["ruby", "-c", fp],
        ".php":  ["php", "-l", fp],
    }
    assert set(sg._CHECKERS) == set(expected)
    for ext, want in expected.items():
        binary, argfn = sg._CHECKERS[ext]
        assert binary == want[0], ext
        assert argfn(binary, fp) == want, ext


def test_kotlin_has_no_external_syntax_checker():
    """Kotlin stays on the heuristic (kotlinc too slow) — absent from _CHECKERS."""
    from aiforge_core.runtime import syntax_guard as sg
    assert ".kt" not in sg._CHECKERS
    assert ".kts" not in sg._CHECKERS
    assert L.PROFILES["kotlin"].syntax_check is None


# ── 5. integration_report gained Kotlin, existing keys intact ──────────

def test_integration_report_kotlin_manual_and_stack_map():
    from aiforge_core.runtime import integration_report as ir
    assert "kotlin" in ir._MANUAL and ir._MANUAL["kotlin"]
    assert ir._STACK_TO_LANG.get("kotlin") == "kotlin"
