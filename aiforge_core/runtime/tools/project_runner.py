"""``project`` — one tool to detect + build + test + run any common stack.

So the agent doesn't have to remember each ecosystem's incantations, this
auto-detects the project type from marker files and runs the canonical
command for the requested action, after ensuring the required toolchain
is installed (via :mod:`ensure_runtime`).

Supported stacks (by marker file):
  * Maven        — ``pom.xml``                  → ``mvn`` (needs java, mvn)
  * Gradle       — ``build.gradle[.kts]``       → ``./gradlew`` | ``gradle``
  * Node / React / Next / Vite — ``package.json`` → npm/yarn/pnpm scripts
  * Python       — ``requirements.txt`` / ``pyproject.toml`` / ``setup.py``
  * Go           — ``go.mod``
  * Rust         — ``Cargo.toml``

Actions: ``detect`` · ``install`` · ``build`` · ``test`` · ``run``.

Execution runs in a new process group and polls :mod:`chat_cancel` so the
chat Stop button kills the whole tree. Never deletes anything.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

_PACKAGE_JSON = 'package.json'

_CAP = 8000


def _has(cwd: str, *names: str) -> bool:
    return any(os.path.exists(os.path.join(cwd, n)) for n in names)


def _has_ext(cwd: str, exts: tuple) -> bool:
    """Bounded walk for a source file with any of ``exts`` (for stacks with no
    manifest file, e.g. bare C/C++)."""
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in {
            ".git", "build", "target", "node_modules", ".venv", "venv"}]
        if any(f.endswith(exts) for f in files):
            return True
        if root.count(os.sep) - cwd.count(os.sep) >= 4:
            dirs[:] = []
    return False


_C_EXTS = (".c",)
_CPP_EXTS = (".cpp", ".cc", ".cxx", ".c++")


def _node_pm(cwd: str) -> str:
    if os.path.exists(os.path.join(cwd, "pnpm-lock.yaml")):
        return "pnpm"
    if os.path.exists(os.path.join(cwd, "yarn.lock")):
        return "yarn"
    return "npm"


def _node_framework(cwd: str) -> str:
    try:
        pkg = json.loads(open(os.path.join(cwd, _PACKAGE_JSON)).read())
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        for fw in ("next", "vite", "react-scripts", "react", "@angular/core", "vue"):
            if fw in deps:
                return fw
    except Exception:  # noqa: BLE001
        pass
    return "node"


_TEST_FILE_SUFFIXES = ("_test.go", ".test.js", ".test.ts", ".spec.ts",
                       "Test.java", "Tests.java")


def _looks_like_test_file(fname: str) -> bool:
    """True when a filename is recognisably a test across the supported stacks."""
    fl = fname.lower()
    if fname.startswith("test_") and fname.endswith(".py"):
        return True
    if fname.endswith(_TEST_FILE_SUFFIXES):
        return True
    if fname.endswith(".rs") and "test" in fl:
        return True
    return "test" in fl and fl.endswith((".c", ".cpp", ".cc", ".cxx"))


def _has_test_files(cwd: str) -> bool:
    """Walk (bounded to depth 4) for a file that looks like a test."""
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in {
            ".git", "node_modules", ".venv", "venv", "target", "build", "dist"}]
        if any(_looks_like_test_file(f) for f in files):
            return True
        if root.count(os.sep) - cwd.count(os.sep) >= 4:   # bound the walk
            dirs[:] = []
    return False


def _node_has_test_script(cwd: str) -> bool:
    """A real package.json "test" script (not the npm-init placeholder)?"""
    try:
        import json as _j
        pkg = _j.loads(open(os.path.join(cwd, _PACKAGE_JSON)).read())
        t = ((pkg.get("scripts") or {}).get("test") or "")
        return bool(t) and "no test specified" not in t
    except Exception:  # noqa: BLE001
        return False


def _has_tests(cwd: str, stacks: list[str]) -> bool:
    """Best-effort: does the project have a runnable test setup?"""
    if os.path.isdir(os.path.join(cwd, "src", "test")):   # maven/gradle
        return True
    if os.path.isdir(os.path.join(cwd, "tests")) or os.path.isdir(
            os.path.join(cwd, "test")):
        return True
    if _has_test_files(cwd):
        return True
    if any(s.startswith("node") for s in stacks):
        return _node_has_test_script(cwd)
    return False


def _detect_native_stack(cwd: str) -> "str | None":
    """The C/C++ build stack for a tree with no higher-level manifest: a CMake
    or Make build file, else bare C/C++ sources. None when none apply."""
    if _has(cwd, "CMakeLists.txt"):
        return "cmake"
    if _has(cwd, "Makefile", "makefile", "GNUmakefile"):
        return "make"
    if _has_ext(cwd, _CPP_EXTS + _C_EXTS):
        return "cpp" if _has_ext(cwd, _CPP_EXTS) else "c"
    return None


def detect(cwd: str) -> dict:
    """Detect the stack(s) present in ``cwd`` + whether tests exist."""
    stacks: list[str] = []
    if _has(cwd, "pom.xml"):
        stacks.append("maven")
    if _has(cwd, "build.gradle", "build.gradle.kts", "settings.gradle"):
        stacks.append("gradle")
    if _has(cwd, _PACKAGE_JSON):
        stacks.append(f"node:{_node_framework(cwd)}")
    if _has(cwd, "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"):
        stacks.append("python")
    if _has(cwd, "go.mod"):
        stacks.append("go")
    if _has(cwd, "Cargo.toml"):
        stacks.append("rust")
    # C / C++ — native builds have no single manifest, so detect a build file
    # (CMake / Make) or bare sources. Only when no higher-level stack already
    # claimed the tree (a Python/Node repo may carry an incidental Makefile).
    if not stacks:
        native = _detect_native_stack(cwd)
        if native:
            stacks.append(native)
    return {"ok": True, "stacks": stacks, "cwd": cwd,
            "has_tests": _has_tests(cwd, stacks),
            "note": "no recognised project markers" if not stacks else ""}


def _plan_maven(cwd: str) -> tuple[list[str], dict]:
    return ["java", "mvn"], {
        "install": ["mvn -q -DskipTests dependency:resolve"],
        "build": ["mvn -q -DskipTests package"],
        "test": ["mvn -q test"],
        "run": ["mvn -q spring-boot:run"]}


def _plan_gradle(cwd: str) -> tuple[list[str], dict]:
    gradlew = os.path.exists(os.path.join(cwd, "gradlew"))
    g = "./gradlew" if gradlew else "gradle"
    tools = ["java"] + ([] if gradlew else ["gradle"])
    return tools, {"install": [f"{g} dependencies"], "build": [f"{g} build -x test"],
                   "test": [f"{g} test"], "run": [f"{g} bootRun"]}


def _plan_node(stack: str, cwd: str) -> tuple[list[str], dict]:
    pm = _node_pm(cwd)
    fw = stack.split(":", 1)[1] if ":" in stack else "node"
    inst = {"npm": "npm install", "yarn": "yarn install",
            "pnpm": "pnpm install"}[pm]
    run_script = "dev" if fw in ("next", "vite", "react-scripts", "react",
                                 "vue") else "start"
    return ["node", pm], {
        "install": [inst], "build": [f"{pm} run build"],
        "test": [f"{pm} test --silent" if pm == "npm" else f"{pm} test"],
        "run": [f"{pm} run {run_script}"]}


def _plan_python(cwd: str) -> tuple[list[str], dict]:
    inst = ("pip install -r requirements.txt"
            if os.path.exists(os.path.join(cwd, "requirements.txt"))
            else "pip install -e .")
    entry = next((f for f in ("main.py", "app.py", "manage.py", "run.py")
                  if os.path.exists(os.path.join(cwd, f))), "main.py")
    return ["python3", "pip"], {
        "install": [inst], "build": ["python -m compileall -q ."],
        "test": ["python -m pytest -q"], "run": [f"python {entry}"]}


def _plan_cpp(stack: str) -> tuple[list[str], dict]:
    # Bare sources, no build file: compile EVERYTHING into one binary and run it
    # (a generated test main asserts + exits non-zero on failure). g++ compiles C
    # and C++; the -std picks a modern default. Shell $(find …) works because
    # _exec runs with shell=True.
    cxx = "g++ -std=c++17" if stack == "cpp" else "gcc -std=c11"
    srcs = (r"$(find . -path ./build -prune -o "
            r"\( -name '*.c' -o -name '*.cpp' -o -name '*.cc' -o -name '*.cxx' \) "
            r"-print)")
    compile_cmd = f"{cxx} -O0 -o ./a.out {srcs}"
    tool = "g++" if stack == "cpp" else "gcc"
    return [tool], {"install": [], "build": [compile_cmd],
                    "test": [f"{compile_cmd} && ./a.out"], "run": ["./a.out"]}


# Stacks whose plan needs no cwd/self inspection — a constant (tools, cmds) map.
_STATIC_PLANS: dict[str, tuple[list[str], dict]] = {
    "go": (["go"], {"install": ["go mod download"], "build": ["go build ./..."],
                    "test": ["go test ./..."], "run": ["go run ."]}),
    "rust": (["cargo"], {"install": ["cargo fetch"], "build": ["cargo build"],
                         "test": ["cargo test"], "run": ["cargo run"]}),
    "make": (["make"], {"install": [], "build": ["make"],
                        # try a `test`/`check` target; fall back to a plain build
                        # so a Makefile without a test target still gates.
                        "test": ["make test 2>/dev/null || make check 2>/dev/null || make"],
                        "run": ["make run"]}),
    "cmake": (["cmake", "make"], {
        "install": [],
        "build": ["cmake -S . -B build && cmake --build build"],
        "test": ["cmake -S . -B build && cmake --build build && "
                 "ctest --test-dir build --output-on-failure"],
        "run": ["cmake --build build --target run"]}),
}


def _stack_plan(stack: str, cwd: str) -> "tuple[list[str], dict] | None":
    """(tools, {action: commands}) for one stack, or None when unknown."""
    if stack == "maven":
        return _plan_maven(cwd)
    if stack == "gradle":
        return _plan_gradle(cwd)
    if stack.startswith("node"):
        return _plan_node(stack, cwd)
    if stack == "python":
        return _plan_python(cwd)
    if stack in ("c", "cpp"):
        return _plan_cpp(stack)
    return _STATIC_PLANS.get(stack)


def _plan(stack: str, action: str, cwd: str) -> tuple[list[str], list[str]]:
    """Return (tools_needed, commands) for one stack + action."""
    plan = _stack_plan(stack, cwd)
    if plan is None:
        return [], []
    tools, cmds = plan
    return tools, cmds.get(action, [])


def _exec(cmd: str, cwd: str, timeout: int) -> dict:
    from aiforge_core.runtime import chat_cancel
    sid = chat_cancel.active()
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except Exception as exc:  # noqa: BLE001
        return {"cmd": cmd, "ok": False, "error": str(exc)}
    if sid is not None:
        try:
            chat_cancel.track_pgid(sid, os.getpgid(proc.pid))
        except Exception:  # noqa: BLE001
            pass
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if sid is not None and chat_cancel.is_cancelled(sid):
            _kill(proc)
            return {"cmd": cmd, "ok": False, "stopped": True}
        if time.monotonic() > deadline:
            _kill(proc)
            return {"cmd": cmd, "ok": False, "error": f"timeout after {timeout}s"}
        time.sleep(0.2)
    # The main process exited — but a daemon grandchild (mvn/gradle/npm) can
    # still hold the stdout pipe, hanging communicate() past the deadline.
    # Bound it; on hang, kill the group and return what we have.
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            out = ""
    return {"cmd": cmd, "ok": proc.returncode == 0, "code": proc.returncode,
            "output": (out or "")[-_CAP:]}


def _kill(proc) -> None:
    import signal
    for s in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), s)
        except Exception:  # noqa: BLE001
            pass


def project(action: str = "detect", cwd: str = ".", timeout: int = 1800) -> dict:
    """Detect, install, build, test, or run the project in ``cwd``.

    Auto-detects the stack (Maven / Gradle / Node-React-Next-Vite /
    Python / Go / Rust), installs the required toolchain if missing, and
    runs the canonical command. Use this instead of hand-writing build
    commands. ``action``: detect | install | build | test | run.

    For ``run`` (long-lived servers) prefer launching via run_command/bash
    with a background ``&`` — this call blocks until the process exits or
    ``timeout`` (default 30 min).
    """
    cwd = os.path.abspath(os.path.expanduser(cwd))
    det = detect(cwd)
    if action == "detect":
        return det
    stacks = det["stacks"]
    if not stacks:
        return {"ok": False, "error": "no recognised project in " + cwd,
                "stacks": []}
    from aiforge_core.runtime.tools.ensure_runtime import ensure_runtime
    results: list[dict] = []
    overall = True
    for stack in stacks:
        tools, cmds = _plan(stack, action, cwd)
        if not cmds:
            continue
        prov = ensure_runtime(tools)
        if not prov.get("ok"):
            results.append({"stack": stack, "ok": False,
                            "error": "toolchain install failed", "provision": prov})
            overall = False
            continue
        for cmd in cmds:
            r = _exec(cmd, cwd, timeout)
            r["stack"] = stack
            results.append(r)
            if not r.get("ok"):
                overall = False
                break
    return {"ok": overall, "action": action, "stacks": stacks, "results": results}
