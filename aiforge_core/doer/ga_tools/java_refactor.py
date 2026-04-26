"""OpenRewrite Java refactor recipes via Maven.

Wires the Moderne OpenRewrite mvn plugin so the Doer can invoke
canonical Java refactors without writing diff blocks:

- ``org.openrewrite.java.ChangePackage`` — rename packages
- ``org.openrewrite.java.RemoveUnusedImports``
- ``org.openrewrite.java.OrderImports``
- ``org.openrewrite.java.RenameMethod`` — rename a method everywhere
- ``org.openrewrite.java.AddImport`` — add an import to all files
- ``org.openrewrite.java.spring.boot3.AddSpringProperty``
- ``org.openrewrite.java.testing.junit5.JUnit5UpgradeRecipe``

Recipe + args come straight from the Moderne docs; the model passes
``recipe`` + ``options`` and we shell out to mvn. Output is the
mvn tail (last 60 lines) so the model sees what changed.

Why this beats hand-patching: package renames touch dozens of
files, including imports across the repo. A single
ChangePackage recipe handles all of them; the doer would burn
~20 turns trying to file_patch each one. Recipes also auto-fix
imports, alphabetise, and respect Java syntax — which our
text-protocol file_patch can't.
"""
from __future__ import annotations

import shutil
import subprocess

SCHEMA = {
    "type": "function",
    "function": {
        "name": "java_refactor",
        "description": (
            "Run a Moderne OpenRewrite recipe via mvn rewrite:run "
            "in the worktree. Recipes handle bulk Java refactors "
            "(ChangePackage, RenameMethod, RemoveUnusedImports, "
            "OrderImports, AddImport, JUnit5UpgradeRecipe). Cheaper "
            "than hand-patching every import + reference — recipe "
            "engine knows Java syntax. Returns mvn tail output so "
            "you can confirm the diff."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipe": {
                    "type": "string",
                    "description": (
                        "Fully-qualified recipe name, e.g. "
                        "'org.openrewrite.java.ChangePackage'."
                    ),
                },
                "options": {
                    "type": "object",
                    "description": (
                        "Recipe-specific options. ChangePackage takes "
                        "{oldPackageName, newPackageName, recursive}. "
                        "RenameMethod takes "
                        "{methodPattern, newMethodName}. See "
                        "https://docs.openrewrite.org/recipes."
                    ),
                },
            },
            "required": ["recipe"],
        },
    },
}


def _mvn_available(worktree: str) -> bool:
    if not shutil.which("mvn"):
        return False
    return True


def _build_mvn_args(recipe: str, options: dict | None) -> list[str]:
    args = [
        "mvn", "-q", "-DskipTests",
        "org.openrewrite.maven:rewrite-maven-plugin:run",
        f"-Drewrite.activeRecipes={recipe}",
        # Pull in the core java + spring recipe modules so most
        # 'org.openrewrite.java.*' names resolve out-of-the-box.
        "-Drewrite.recipeArtifactCoordinates="
        "org.openrewrite.recipe:rewrite-java-dependencies:RELEASE,"
        "org.openrewrite.recipe:rewrite-spring:RELEASE,"
        "org.openrewrite.recipe:rewrite-testing-frameworks:RELEASE",
    ]
    for k, v in (options or {}).items():
        args.append(f"-D{k}={v}")
    return args


def handle(worktree: str, args: dict, *, timeout_s: int = 600) -> str:
    recipe = (args.get("recipe") or "").strip()
    if not recipe:
        return "[java_refactor] recipe required"
    options = args.get("options") or {}
    if not _mvn_available(worktree):
        return "[java_refactor] mvn not on PATH"
    cmd = _build_mvn_args(recipe, options)
    try:
        proc = subprocess.run(
            cmd, cwd=worktree, capture_output=True, text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return f"[java_refactor] timed out after {timeout_s}s"
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    tail = "\n".join(out.splitlines()[-60:])
    status = "ok" if proc.returncode == 0 else f"rc={proc.returncode}"
    return (
        f"[java_refactor] recipe={recipe} {status}\n"
        f"mvn tail:\n{tail[-4000:]}"
    )
