"""Per-language knowledge registry.

Single import point for the language profiles. The three legacy consumers
(``config.repo_standards``, ``runtime.integration_report``,
``runtime.syntax_guard``) source their per-language knowledge here instead of
each carrying its own scattered literals.

Public surface:
- ``PROFILES``           — ``{name: LanguageProfile}`` for every language.
- ``all_profiles()``     — the profiles as a tuple.
- ``by_name(name)``      — exact profile-name lookup (case-insensitive).
- ``by_alias(name)``     — resolve a name OR any registered alias.
- ``by_extension(ext)``  — the profile that owns a file extension.
- ``detect(cwd)``        — fingerprint a worktree → profile name (or None).

``detect`` replicates ``integration_report._detect_lang``'s marker-then-
extension priority (so already-supported detection is unchanged), mapped to
canonical profile names, with Kotlin promoted ahead of the shared JVM markers
so a Kotlin tree is not mislabelled ``java``.
"""
from __future__ import annotations

import os

from aiforge_core.config.languages.base import LanguageProfile
from aiforge_core.config.languages.c import PROFILE as _C
from aiforge_core.config.languages.cpp import PROFILE as _CPP
from aiforge_core.config.languages.java import PROFILE as _JAVA
from aiforge_core.config.languages.kotlin import PROFILE as _KOTLIN
from aiforge_core.config.languages.python import PROFILE as _PYTHON
from aiforge_core.config.languages.rust import PROFILE as _RUST
from aiforge_core.config.languages.shell import PROFILE as _SHELL

# Ordered so iteration is deterministic (name -> profile).
PROFILES: dict[str, LanguageProfile] = {
    p.name: p
    for p in (_PYTHON, _JAVA, _KOTLIN, _SHELL, _C, _CPP, _RUST)
}

# ── reverse indexes ───────────────────────────────────────────────────
_BY_ALIAS: dict[str, LanguageProfile] = {}
_BY_EXT: dict[str, LanguageProfile] = {}
for _p in PROFILES.values():
    _BY_ALIAS[_p.name.lower()] = _p
    for _a in _p.aliases:
        _BY_ALIAS.setdefault(_a.lower(), _p)
    for _e in _p.extensions:
        _BY_EXT.setdefault(_e.lower(), _p)


def all_profiles() -> tuple[LanguageProfile, ...]:
    """Every registered profile, in registry order."""
    return tuple(PROFILES.values())


def by_name(name: str | None) -> LanguageProfile | None:
    """Exact profile-name lookup (case-insensitive). None if unknown."""
    if not name:
        return None
    return PROFILES.get(name.strip().lower())


def by_alias(name: str | None) -> LanguageProfile | None:
    """Resolve a canonical name OR any registered alias. None if unknown."""
    if not name:
        return None
    return _BY_ALIAS.get(name.strip().lower())


def by_extension(ext: str | None) -> LanguageProfile | None:
    """Profile that owns a file extension (leading dot optional)."""
    if not ext:
        return None
    ext = ext.strip().lower()
    if not ext.startswith("."):
        ext = "." + ext
    return _BY_EXT.get(ext)


# Directories skipped while sniffing extensions — identical to
# integration_report._detect_lang so detection stays byte-for-byte in sync.
_SKIP_DIRS = frozenset((
    ".git", ".venv", "venv", "node_modules", "target", "build", "dist",
    ".aiforge-worktrees", "__pycache__",
))


def _collect_exts(cwd: str) -> set[str]:
    exts: set[str] = set()
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            exts.add(os.path.splitext(f)[1].lower())
    return exts


# Marker file → profile, most-specific first. A table rather than a chain of
# ifs: the ordering IS the logic, and a table shows it at a glance where ten
# near-identical branches hid it. ``None`` means "recognised, but not
# first-class here" — still authoritative, so detection stops.
_MARKERS: "tuple[tuple[tuple[str, ...], str | None], ...]" = (
    (("pom.xml",), "java"),
    (("build.gradle", "build.gradle.kts", "settings.gradle"), "java"),
    (("go.mod",), None),
    (("Cargo.toml",), "rust"),
    (("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"), "python"),
    (("package.json",), None),
    (("composer.json",), None),
    (("Gemfile",), None),
    (("CMakeLists.txt", "Makefile"), "cpp"),
)


def detect(cwd: str | None) -> str | None:
    """Fingerprint a worktree → canonical profile name (or None).

    Mirrors ``integration_report._detect_lang``'s marker-then-extension
    priority, mapped to profile names. Languages that repo detection knows but
    that are NOT first-class here (go / node / php / ruby) resolve to None.
    Kotlin (.kt/.kts) is checked first so a Kotlin tree that also carries the
    shared ``build.gradle(.kts)`` marker is not mislabelled ``java``.
    """
    if not cwd or not os.path.isdir(cwd):
        return None

    def has(*names: str) -> bool:
        return any(os.path.exists(os.path.join(cwd, n)) for n in names)

    exts = _collect_exts(cwd)

    # Kotlin first-class — sources win over the shared JVM marker.
    if ".kt" in exts or ".kts" in exts:
        return "kotlin"

    # 1) authoritative marker files, most-specific first (→ profile names).
    for names, profile in _MARKERS:
        if has(*names):
            # A marker MATCHING but mapping to None still stops here: a go.mod
            # tree is Go, not "whatever extensions happen to be lying around".
            return profile

    # 2) no marker — extension fallback, fixed priority (python before node).
    for lang, es in (
        ("python", {".py"}),
        (None, {".go"}),
        ("rust", {".rs"}),
        ("java", {".java"}),
        ("cpp", {".c", ".cpp", ".cc", ".cxx"}),
        (None, {".php"}),
        (None, {".rb"}),
        (None, {".ts", ".tsx", ".js", ".mjs"}),
        ("shell", {".sh", ".bash"}),
    ):
        if exts & es:
            return lang
    return None


__all__ = [
    "LanguageProfile",
    "PROFILES",
    "all_profiles",
    "by_name",
    "by_alias",
    "by_extension",
    "detect",
]
