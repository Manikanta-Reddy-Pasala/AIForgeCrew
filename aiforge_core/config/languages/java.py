"""Java language profile."""
from __future__ import annotations

import os

from aiforge_core.config.languages.base import LanguageProfile

# Commands copied verbatim from repo_standards._DEFAULTS_BY_LANG["java"].
# syntax_check mirrors syntax_guard._CHECKERS[".java"] exactly: javac writes
# class files under the temp dir (-d) and never runs the code.
PROFILE = LanguageProfile(
    name="java",
    aliases=("jvm", "maven", "gradle", "java-maven", "java-gradle"),
    extensions=(".java",),
    build_markers=("pom.xml", "build.gradle", "build.gradle.kts",
                   "settings.gradle"),
    compile_cmd="mvn -q -DskipTests compile",
    test_cmd="mvn test",
    lint_cmd="mvn -q checkstyle:check",
    format_cmd="mvn -q spotless:apply",
    syntax_check=("javac", lambda b, f: [b, "-d", os.path.dirname(f), f]),
    toolchain_candidates={
        "java": ("java",),
        "maven": ("mvn",),
        "gradle": ("gradle",),
    },
    install_hint=("Install a JDK (the repo's build files / first build error "
                  "state which version); Maven (`mvn`) or Gradle (`gradle`)."),
    conventions=(
        "Spring Boot conventions; constructor injection; JUnit 5 + "
        "@MockitoBean (Spring Boot 4) for tests; Jackson 3 is `tools.jackson.*`; "
        "avoid field injection; prefer Optional over null returns."
    ),
)

__all__ = ["PROFILE"]
