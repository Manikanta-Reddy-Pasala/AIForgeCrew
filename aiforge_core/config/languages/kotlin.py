"""Kotlin language profile (first-class, newly added).

Kotlin builds on the JVM. When a Gradle/Maven driver is present the build
tool compiles it (kotlin-maven-plugin / the Gradle Kotlin plugin); a bare
Kotlin tree falls back to the standalone ``kotlinc`` compiler. The static
defaults below assume the common Gradle case — ``repo_standards`` already
routes real Gradle/Maven repos through its host-resolved toolchain, so these
only apply to a marker-less Kotlin tree.

syntax_check is None on purpose: ``kotlinc`` is far too slow for the
pre-write syntax sniff, so ``validate_syntax`` keeps Kotlin on its
brace-balance + Python-kwarg heuristic (it already special-cases ``.kt``).
"""
from __future__ import annotations

from aiforge_core.config.languages.base import LanguageProfile

PROFILE = LanguageProfile(
    name="kotlin",
    aliases=("kt", "kotlin-gradle", "kotlin-maven"),
    extensions=(".kt", ".kts"),
    build_markers=("build.gradle.kts", "settings.gradle.kts", "pom.xml",
                   "build.gradle"),
    # Gradle-first defaults (most Kotlin projects); Maven / kotlinc noted in
    # conventions. Real Gradle/Maven repos are refined by resolve_toolchain.
    compile_cmd="./gradlew compileKotlin -x test",
    test_cmd="./gradlew test",
    lint_cmd="",
    format_cmd="",
    syntax_check=None,
    toolchain_candidates={
        "kotlinc": ("kotlinc",),
        "java": ("java",),
        "gradle": ("gradle",),
        "maven": ("mvn",),
    },
    install_hint=("Install the Kotlin compiler (`kotlinc`) and a JDK; or use "
                  "the repo's Gradle/Maven wrapper (kotlin-maven-plugin / "
                  "Gradle Kotlin DSL)."),
    conventions=(
        "Prefer data classes and immutability (val over var); null-safety with "
        "?/?:/!! avoided; build with Gradle Kotlin DSL or kotlin-maven-plugin, "
        "or `kotlinc` for a bare tree; test with kotlin.test / JUnit 5."
    ),
)

__all__ = ["PROFILE"]
