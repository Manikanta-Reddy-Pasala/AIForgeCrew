"""Finding and checking the build toolchain a repo needs (Python, JVM, Node, native)."""
from __future__ import annotations

import glob as _glob
import os
import shutil


def _pkg():
    """``repo_standards``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``repo_standards``; patch any other
    name on this module."""
    import aiforge_core.config.repo_standards as package
    return package


def _reset_toolchain_cache() -> None:
    """Test-only — clear the probe + lang caches."""
    pkg = _pkg()
    pkg._TOOLCHAIN_CACHE.clear()
    pkg._LANG_CACHE.clear()


def _first_on_path(*candidates: str) -> str | None:
    for c in candidates:
        if shutil.which(c):
            return c
    return None


def _wrapper_or_path(worktree, wrapper: str, *binaries: str) -> str:
    """The checked-in wrapper when present, else the first binary on PATH,
    else the wrapper name (so the error names what is missing)."""
    if worktree and os.path.isfile(os.path.join(worktree, wrapper.lstrip("./"))):
        return wrapper
    return _pkg()._first_on_path(*binaries) or wrapper


def _python_toolchain() -> dict[str, str]:
    py = _pkg()._first_on_path("python3", "python") or "python3"
    return {"compile_cmd": f"{py} -m compileall -q .",
            "test_cmd": f"{py} -m pytest -q"}


def _java_toolchain(worktree) -> dict[str, str]:
    """Gradle (incl. Kotlin/.kts) vs Maven — picked per marker/wrapper so a
    Kotlin/gradle repo doesn't get mvn commands it can't run."""
    pkg = _pkg()
    from aiforge_core.config.safe_paths import safe_dir
    worktree = safe_dir(worktree)
    is_gradle = bool(worktree and _glob.glob(
        os.path.join(worktree, pkg._BUILD_GRADLE)))
    has_pom = bool(worktree and os.path.isfile(
        os.path.join(worktree, pkg._POM_XML)))
    if is_gradle and not has_pom:
        g = _wrapper_or_path(worktree, "./gradlew", "gradle")
        return {"build_cmd": f"{g} build -x test",
                "compile_cmd": f"{g} compileJava compileKotlin -x test",
                "test_cmd": f"{g} test"}
    mvn = _wrapper_or_path(worktree, "./mvnw", "mvn")
    return {"build_cmd": f"{mvn} clean package -DskipTests",
            "compile_cmd": f"{mvn} -q -DskipTests compile",
            "test_cmd": f"{mvn} test"}


def _node_toolchain(worktree) -> dict[str, str]:
    """The package manager the LOCKFILE names, else whatever is installed."""
    for lockfile, pm in (("yarn.lock", "yarn"), ("pnpm-lock.yaml", "pnpm")):
        if worktree and os.path.isfile(os.path.join(worktree, lockfile)):
            return {"build_cmd": f"{pm} run build", "test_cmd": f"{pm} test"}
    pm = _pkg()._first_on_path("npm", "pnpm", "yarn") or "npm"
    return {"build_cmd": f"{pm} run build", "test_cmd": f"{pm} test"}


def resolve_toolchain(lang: str, worktree: str | None = None) -> dict[str, str]:
    """Return host-resolved command overrides for ``lang`` (cached).

    Resolves the actual interpreter/build tool present so the Doer never
    re-discovers it: ``python3`` when ``python`` is absent, the ``./mvnw``
    wrapper when checked in, ``yarn``/``pnpm`` per lockfile, etc. Pure
    ``shutil.which`` + lockfile checks — no subprocess, soft-fails to the
    static default by returning an empty dict.
    """
    pkg = _pkg()
    key = (lang or "", os.path.abspath(worktree) if worktree else "")
    if key in pkg._TOOLCHAIN_CACHE:
        return pkg._TOOLCHAIN_CACHE[key]
    lk = (lang or "").lower()
    try:
        if lk == "python":
            out = _python_toolchain()
        elif lk == "java":
            out = _java_toolchain(worktree)
        elif lk in ("node", "react"):
            out = _node_toolchain(worktree)
        else:
            out = {}
    except Exception:  # noqa: BLE001 — probing must never break standards
        out = {}
    pkg._TOOLCHAIN_CACHE[key] = out
    return out


def _check_jvm_toolchain(worktree, lang, is_maven, is_gradle):
    """Missing JVM-family build tools (java/maven/gradle/standalone kotlinc)."""
    msgs: list[str] = []
    jvm = is_maven or is_gradle or lang == "java"
    if jvm and not shutil.which("java"):
        msgs.append("No `java` on the host — install a JDK (the repo's build "
                    "files / first build error state which version).")
    if is_maven and not (os.path.isfile(os.path.join(worktree, "mvnw"))
                         or shutil.which("mvn")):
        msgs.append("No Maven — install `mvn`, or commit the `mvnw` wrapper.")
    if is_gradle and not (os.path.isfile(os.path.join(worktree, "gradlew"))
                          or shutil.which("gradle")):
        msgs.append("No Gradle — install `gradle`, or commit the `gradlew` "
                    "wrapper.")
    # Standalone kotlinc only matters when Kotlin is the PRIMARY build with no
    # Maven/Gradle driver — otherwise mvn (kotlin-maven-plugin) / gradle compile
    # it. Guarding on `not is_maven and not is_gradle` also avoids the expensive
    # recursive **/*.kt walk on every Java/Maven repo, and stops a bogus
    # "install kotlinc" banner for a Java repo that merely has a stray .kt file.
    if (not is_gradle and not is_maven
            and _glob.glob(os.path.join(worktree, "**", "*.kt"), recursive=True)
            and not shutil.which("kotlinc")):
        msgs.append("Kotlin sources but no build driver and no `kotlinc` — "
                    "install the Kotlin compiler.")
    return msgs


def _check_native_toolchain(worktree, lang):
    """Missing native/other build tools (rust, c/c++ + cmake/make, shell)."""
    msgs: list[str] = []
    # Rust
    if os.path.isfile(os.path.join(worktree, "Cargo.toml")) \
            and not shutil.which("cargo"):
        msgs.append("Rust repo but no `cargo` — install the Rust toolchain "
                    "(rustup).")
    # C / C++ — need a compiler, plus the build driver the repo uses
    if lang in ("c", "cpp"):
        if not (shutil.which("cc") or shutil.which("gcc")
                or shutil.which("clang")):
            msgs.append("C/C++ repo but no compiler — install gcc or clang.")
        if os.path.isfile(os.path.join(worktree, "CMakeLists.txt")) \
                and not shutil.which("cmake"):
            msgs.append("CMake build but no `cmake` — install CMake.")
        elif (os.path.isfile(os.path.join(worktree, "Makefile"))
              or os.path.isfile(os.path.join(worktree, "makefile"))) \
                and not shutil.which("make"):
            msgs.append("Makefile build but no `make` — install make "
                        "(build-essential).")
    # Shell — bash to run, shellcheck to lint (optional; only warn if scripts
    # exist and neither bash nor sh is present, which is essentially never).
    if lang == "shell" and not (shutil.which("bash") or shutil.which("sh")):
        msgs.append("Shell repo but no `bash`/`sh` — install bash.")
    return msgs


def check_toolchain(worktree: str | None,
                    lang: str | None = None) -> list[str]:
    """Preflight: which build tools the repo needs are ENTIRELY ABSENT from the
    host (dynamic ``shutil.which`` — no hardcoded versions). Empty = tools present.

    ``lang`` may be passed by the caller (``toolchain_brief`` already resolved
    it) to avoid re-running ``detect_lang``'s tree walk twice per seed.

    Only presence is checked here. VERSION mismatches (e.g. the repo compiles
    with a newer JDK than the host has) are NOT guessed from build-file regex —
    they surface dynamically at real build time, and the Doer is instructed to
    read that actual error and report the install need (see the toolchain rule
    in the doer prompt / seed). This keeps the check truthful and un-hardcoded:
    a missing binary is unambiguous; a version requirement is whatever the build
    tool itself reports when run.
    """
    pkg = _pkg()
    if not worktree or not os.path.isdir(worktree):
        return []
    msgs: list[str] = []
    is_maven = os.path.isfile(os.path.join(worktree, pkg._POM_XML))
    is_gradle = bool(_glob.glob(os.path.join(worktree, pkg._BUILD_GRADLE)))
    lang = lang if lang is not None else pkg.detect_lang(worktree)
    msgs += _check_jvm_toolchain(worktree, lang, is_maven, is_gradle)
    msgs += _check_native_toolchain(worktree, lang)
    return msgs


def toolchain_brief(worktree: str | None) -> str:
    """Doer-facing 'use these, don't re-discover' block of host-resolved
    commands for the repo at ``worktree``. Empty when the language can't
    be fingerprinted. Seeded into doer state so the agent never re-probes
    python/python3/build tools per ticket. Leads with a MISSING-TOOLCHAIN
    banner when :func:`check_toolchain` finds an uninstalled/mismatched tool."""
    if not worktree:
        return ""
    lang = _pkg().detect_lang(worktree)          # resolved ONCE; passed to check below
    if not lang:
        return ""
    missing = check_toolchain(worktree, lang)
    banner = ""
    if missing:
        banner = ("⚠ MISSING TOOLCHAIN — install these yourself via the host's "
                  "version/package manager (sdkman/nvm/pyenv/apt/brew) and set "
                  "the default, then build; do NOT fake a green compile:\n"
                  + "\n".join(f"- {m}" for m in missing) + "\n\n")
    tc = resolve_toolchain(lang, worktree)
    if not tc and not banner:
        return ""
    lines = [f"- {k.replace('_cmd', '').replace('_', ' ')}: `{v}`"
             for k, v in tc.items()]
    body = (
        f"DETECTED TOOLCHAIN ({lang}, host-verified — use these EXACT "
        "commands; do NOT re-probe for python/python3 or the build tool):\n"
        + "\n".join(lines)
    ) if tc else ""
    return banner + body
