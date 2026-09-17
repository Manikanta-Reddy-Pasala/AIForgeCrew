"""Per-project standards / tools / tests catalogue.

Single source of truth for "what does dev activity look like in this
repo?" — used by every Doer tool that builds, tests, lints, formats,
or refactors. Replaces the patchwork of hardcoded ``mvn`` lines in
``ga_tools/{lint,tests,java_refactor}.py``.

Storage layout (KISS): per-language defaults, overlaid by a
per-worktree YAML file:

**``.aiforge/aiforge.conf.yml``** in the worktree provides a
per-tree override for ad-hoc experiments. Worktree YAML wins on
conflict — operators iterate locally without touching the defaults.

Public surface (KISS):
- ``get(repo_name, *, worktree=None) -> Standards``
- ``render(std) -> str``
- ``apply_to_env(std)`` — lift ``lint_cmd`` / ``test_cmd`` / etc.
  into env so legacy ga_tools that read AIFORGE_LINT_CMD pick them
  up without code change.
"""
from __future__ import annotations

import glob as _glob
import os
import shutil  # noqa: F401  # tests patch repo_standards.shutil.which
from dataclasses import dataclass, field, fields

from aiforge_core.config import languages as _languages

from ._repo_toolchain import (  # noqa: F401  # re-exported
    _check_jvm_toolchain,
    _check_native_toolchain,
    _first_on_path,
    _java_toolchain,
    _node_toolchain,
    _python_toolchain,
    _reset_toolchain_cache,
    _wrapper_or_path,
    check_toolchain,
    resolve_toolchain,
    toolchain_brief,
)

_BUILD_GRADLE = 'build.gradle*'
_POM_XML = 'pom.xml'


@dataclass
class Standards:
    """Resolved per-project standards manifest."""
    name: str = ""
    lang: str = ""
    stack: list[str] = field(default_factory=list)
    dockerfile: bool = False
    ports: list[int] = field(default_factory=list)
    # Dev-activity commands (each may be empty → tool falls back to
    # its built-in default).
    entry_cmd: str = ""
    build_cmd: str = ""
    compile_cmd: str = ""
    test_cmd: str = ""
    lint_cmd: str = ""
    format_cmd: str = ""
    security_scan_cmd: str = ""
    # Quality + safety rails.
    conventions: list[str] = field(default_factory=list)
    forbidden_patterns: list[str] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    # Provenance.
    source: str = "default"  # 'worktree' | 'auto-detect' | 'default'


# Sensible per-language defaults so brand-new repos still work
# without operator setup.
_DEFAULTS_BY_LANG: dict[str, dict[str, str]] = {
    "java": {
        "build_cmd":         "./mvnw clean package -DskipTests",
        "compile_cmd":       "mvn -q -DskipTests compile",
        "test_cmd":          "mvn test",
        "lint_cmd":          "mvn -q checkstyle:check",
        "format_cmd":        "mvn -q spotless:apply",
        "security_scan_cmd": "mvn -q org.owasp:dependency-check-maven:check",
    },
    "python": {
        "build_cmd":         "uv sync",
        "compile_cmd":       "python -m compileall -q .",
        "test_cmd":          "python -m pytest -q",
        "lint_cmd":          "ruff check .",
        "format_cmd":        "ruff format .",
        "security_scan_cmd": "bandit -q -r .",
    },
    "node": {
        "build_cmd":         "npm run build",
        "compile_cmd":       "tsc --noEmit",
        "test_cmd":          "npm test",
        "lint_cmd":          "npm run lint",
        "format_cmd":        "npx prettier --write .",
        "security_scan_cmd": "npm audit --audit-level=high",
    },
    "go": {
        "build_cmd":         "go build ./...",
        "compile_cmd":       "go build ./...",
        "test_cmd":          "go test ./...",
        "lint_cmd":          "go vet ./...",
        "format_cmd":        "gofmt -w .",
        "security_scan_cmd": "govulncheck ./...",
    },
    "rust": {
        "build_cmd":         "cargo build",
        "compile_cmd":       "cargo check",
        "test_cmd":          "cargo test",
        "lint_cmd":          "cargo clippy -- -D warnings",
        "format_cmd":        "cargo fmt",
        "security_scan_cmd": "cargo audit",
    },
    # Bootstrap defaults only — the real build/test come from learning
    # (repo_catalog). These are last-resort fallbacks so a fresh C/C++/shell
    # repo still runs. resolve_toolchain refines them to what's on the host.
    "c": {
        "build_cmd":   "make",
        "compile_cmd": "make",
        "test_cmd":    "make test",
        "lint_cmd":    "",
        "format_cmd":  "clang-format -i **/*.c **/*.h",
    },
    "cpp": {
        "build_cmd":   "cmake --build build",
        "compile_cmd": "cmake --build build",
        "test_cmd":    "ctest --output-on-failure --test-dir build",
        "lint_cmd":    "",
        "format_cmd":  "clang-format -i **/*.cpp **/*.hpp",
    },
    "shell": {
        "build_cmd":   "",
        "compile_cmd": "bash -n",          # syntax check (per-file via resolve)
        "test_cmd":    "bats .",
        "lint_cmd":    "shellcheck",
        "format_cmd":  "shfmt -w .",
    },
    "react": {
        "build_cmd":         "yarn install && yarn build",
        "compile_cmd":       "yarn tsc --noEmit",
        "test_cmd":          "yarn test --watchAll=false",
        "lint_cmd":          "yarn lint",
        "format_cmd":        "yarn prettier --write src",
        "security_scan_cmd": "yarn audit --level high",
    },
}

# Kotlin (first-class) — sourced from the language registry so its command
# knowledge lives in one place (aiforge_core/config/languages/kotlin.py). The
# LanguageProfile doesn't model build_cmd (that's a Gradle-oriented default
# supplied here); compile/test/lint/format come straight from the profile. A
# Kotlin repo with a real Gradle/Maven driver is refined by resolve_toolchain's
# JVM branch; this default only backstops a marker-less Kotlin tree.
_kotlin_profile = _languages.PROFILES["kotlin"]
_DEFAULTS_BY_LANG["kotlin"] = {
    "build_cmd":   "./gradlew build -x test",
    "compile_cmd": _kotlin_profile.compile_cmd,
    "test_cmd":    _kotlin_profile.test_cmd,
    "lint_cmd":    _kotlin_profile.lint_cmd,
    "format_cmd":  _kotlin_profile.format_cmd,
}


# ───────── toolchain probe (cache discovered interpreters/tools) ──────
#
# The static defaults above hardcode ``python`` / ``mvn``. On a host where
# only ``python3`` exists, that makes the Doer run ``python …``, fail, then
# re-discover ``python3`` from scratch EVERY ticket. We probe the real
# tool ONCE per (lang, worktree), cache it, and feed it into the manifest
# so the injected command matches reality and is never re-discovered.
_TOOLCHAIN_CACHE: dict[tuple[str, str], dict[str, str]] = {}


def detect_lang(worktree_path: str) -> str:
    """Best-effort language fingerprint based on marker files in *worktree_path*.

    Detection rules (highest priority first):
      * ``pom.xml`` or any ``build.gradle*`` → ``"java"``
      * ``package.json``                     → ``"node"``
      * ``go.mod``                           → ``"go"``
      * ``pyproject.toml`` or ``requirements.txt`` → ``"python"``

    Returns ``""`` when no marker is found — callers should NOT then
    silently fall back to a Java toolchain. The Doer's pre-flight gate
    is wired to skip-with-warn instead.
    """
    if not worktree_path:
        return ""
    # Resolve BEFORE anything is joined to it: the value arrives from a ticket
    # or from Settings, i.e. across an HTTP boundary, and `/repo/../etc` has to
    # become `/etc` while the answer can still be "not a directory, nothing to
    # detect" rather than a walk of somewhere else.
    from aiforge_core.config.safe_paths import safe_dir
    base = safe_dir(worktree_path)
    if not base:
        return ""
    cached = _LANG_CACHE.get(base)
    if cached is not None:
        return cached
    lang = _detect_lang_uncached(base)
    _LANG_CACHE[base] = lang
    return lang


# detect_lang can walk recursive ``**`` globs (C/C++/shell fallback); memoize
# per abspath so ``toolchain_brief`` → ``check_toolchain`` don't re-walk a big
# tree twice per seed. Cleared by tests via _reset_toolchain_cache.
_LANG_CACHE: dict[str, str] = {}


def _detect_native_lang(base):
    """C++/C (via sources or a make/cmake build with headers), shell, or a bare
    Kotlin tree — the marker-less languages, checked after all build-file
    markers. Returns the language or ''."""
    # C / C++ — C++ source/header wins (a C++ project usually also has .c/.h);
    # else any .c source OR a make/cmake build (headers-only C libs) → C.
    cpp_src = _glob.glob(os.path.join(base, "**", "*.cpp"), recursive=True) \
        or _glob.glob(os.path.join(base, "**", "*.cc"), recursive=True) \
        or _glob.glob(os.path.join(base, "**", "*.cxx"), recursive=True) \
        or _glob.glob(os.path.join(base, "**", "*.hpp"), recursive=True)
    if cpp_src:
        return "cpp"
    c_src = _glob.glob(os.path.join(base, "**", "*.c"), recursive=True)
    has_make = (os.path.isfile(os.path.join(base, "CMakeLists.txt"))
                or os.path.isfile(os.path.join(base, "Makefile"))
                or os.path.isfile(os.path.join(base, "makefile")))
    # .c sources → C; or a make/cmake build WITH C headers (a headers-only C
    # lib). A bare Makefile with no C evidence is NOT enough (a shell repo may
    # ship a Makefile) — fall through to the shell check.
    c_hdr = _glob.glob(os.path.join(base, "**", "*.h"), recursive=True)
    if c_src or (has_make and c_hdr):
        return "c"
    # Shell — a repo of scripts (no other build system matched above).
    if _glob.glob(os.path.join(base, "**", "*.sh"), recursive=True) \
            or _glob.glob(os.path.join(base, "**", "*.bash"), recursive=True):
        return "shell"
    # Kotlin (first-class) — a bare Kotlin tree with no build driver. Placed
    # LAST so no previously-matched language changes: gradle/maven markers
    # already return "java" above, and any repo that matched earlier keeps its
    # result. Only a marker-less .kt/.kts tree (formerly "") becomes "kotlin".
    if _glob.glob(os.path.join(base, "**", "*.kt"), recursive=True) \
            or _glob.glob(os.path.join(base, "**", "*.kts"), recursive=True):
        return "kotlin"
    return ""


def _detect_lang_uncached(base: str) -> str:
    if not os.path.isdir(base):
        return ""
    if os.path.isfile(os.path.join(base, _POM_XML)):
        return "java"
    if _glob.glob(os.path.join(base, _BUILD_GRADLE)):
        return "java"
    if os.path.isfile(os.path.join(base, "package.json")):
        return "node"
    if os.path.isfile(os.path.join(base, "go.mod")):
        return "go"
    if os.path.isfile(os.path.join(base, "Cargo.toml")):
        return "rust"
    if (
        os.path.isfile(os.path.join(base, "pyproject.toml"))
        or os.path.isfile(os.path.join(base, "requirements.txt"))
    ):
        return "python"
    return _detect_native_lang(base)


def get(repo_name: str, *, worktree: str | None = None) -> Standards:
    """Return the merged manifest for ``repo_name``.

    Resolution order (highest priority last):
      1. Per-language defaults (always present)
      2. ``<worktree>/.aiforge/aiforge.conf.yml`` (operator override)
    """
    std = Standards(name=repo_name)
    if worktree:
        _apply(std, _from_worktree(worktree))
    # Last-resort lang fallback: only fire when the worktree YAML did not
    # supply an explicit ``lang``. Don't override an operator-set lang —
    # that's the whole point of the override layer.
    if not (std.lang or "").strip() and worktree:
        guessed = detect_lang(worktree)
        if guessed:
            std.lang = guessed
            if std.source == "default":
                std.source = "auto-detect"
    _apply_defaults(std, worktree)
    if not std.source:
        std.source = "default"
    return std


def render(std: Standards) -> str:
    """Compact prompt-friendly rendering."""
    parts = [f"[standards] {std.name} · lang={std.lang or '?'} · "
             f"source={std.source}"]
    for key in (
        "build_cmd", "compile_cmd", "test_cmd", "lint_cmd",
        "format_cmd", "security_scan_cmd", "entry_cmd",
    ):
        val = getattr(std, key)
        if val:
            parts.append(f"  {key:18s}= {val}")
    if std.conventions:
        parts.append(f"  conventions       = {len(std.conventions)} rules")
    if std.forbidden_patterns:
        parts.append(
            f"  forbidden         = {', '.join(std.forbidden_patterns[:5])}"
        )
    if std.acceptance_criteria:
        parts.append(
            f"  acceptance        = {len(std.acceptance_criteria)} item(s)"
        )
    return "\n".join(parts)


def apply_to_env(std: Standards) -> None:
    """Lift commands to env so legacy ga_tools see them.

    Mapping (manifest field → env var):
      build_cmd          → AIFORGE_BUILD_CMD
      compile_cmd        → AIFORGE_COMPILE_CMD
      test_cmd           → AIFORGE_TEST_CMD
      lint_cmd           → AIFORGE_LINT_CMD
      format_cmd         → AIFORGE_FORMAT_CMD
      security_scan_cmd  → AIFORGE_SECURITY_SCAN_CMD

    Existing env values WIN — operator pinning is preserved.
    """
    pairs = (
        ("build_cmd",         "AIFORGE_BUILD_CMD"),
        ("compile_cmd",       "AIFORGE_COMPILE_CMD"),
        ("test_cmd",          "AIFORGE_TEST_CMD"),
        ("lint_cmd",          "AIFORGE_LINT_CMD"),
        ("format_cmd",        "AIFORGE_FORMAT_CMD"),
        ("security_scan_cmd", "AIFORGE_SECURITY_SCAN_CMD"),
    )
    for attr, env in pairs:
        val = getattr(std, attr)
        if val and not os.environ.get(env):
            os.environ[env] = val


# ───────── helpers ────────────────────────────────────────────────


def _apply(std: Standards, src: dict | None) -> None:
    if not src:
        return
    valid = {f.name for f in fields(Standards)}
    for k, v in src.items():
        if k not in valid:
            continue
        if v in (None, "", []):
            continue
        setattr(std, k, v)
    if "source" in src:
        std.source = src["source"]


def _apply_defaults(std: Standards, worktree: str | None = None) -> None:
    lang_key = (std.lang or "").lower()
    if lang_key not in _DEFAULTS_BY_LANG:
        return
    # Static defaults, then overlay the host-resolved toolchain (python3 vs
    # python, ./mvnw vs mvn, yarn vs npm) so the injected commands match the
    # machine and the Doer never re-discovers them. Operator/worktree
    # values set earlier still win — we only fill EMPTY fields.
    merged = dict(_DEFAULTS_BY_LANG[lang_key])
    merged.update(resolve_toolchain(lang_key, worktree))
    for k, v in merged.items():
        if not getattr(std, k):
            setattr(std, k, v)


def _from_worktree(worktree: str) -> dict | None:
    """Read per-worktree YAML override from <worktree>/.aiforge/aiforge.conf.yml.
    Replaces the old ga_tools.repo_config.load() call inline — no new deps."""
    import yaml as _yaml

    from aiforge_core.config.safe_paths import safe_dir
    worktree = safe_dir(worktree)
    if not worktree:
        return None
    conf = os.path.join(worktree, ".aiforge", "aiforge.conf.yml")
    if not os.path.isfile(conf):
        return None
    try:
        with open(conf, "r", encoding="utf-8") as fh:
            data = _yaml.safe_load(fh) or {}
    except Exception:
        return None
    if not data:
        return None
    data["source"] = "worktree"
    return _coerce(data)


def _coerce(row: dict) -> dict:
    """Whitelist + light type-cast so junk fields don't poison
    the dataclass hydrate."""
    valid = {f.name for f in fields(Standards)}
    out: dict = {}
    for k, v in row.items():
        if k not in valid:
            continue
        # Pretend "stack" / "ports" can arrive as a string ("java,maven").
        if k in ("stack", "conventions", "forbidden_patterns",
                 "env_vars", "acceptance_criteria") and isinstance(v, str):
            v = [s.strip() for s in v.split(",") if s.strip()]
        elif k == "ports" and isinstance(v, str):
            try:
                v = [int(p.strip()) for p in v.split(",") if p.strip()]
            except ValueError:
                v = []
        out[k] = v
    return out
