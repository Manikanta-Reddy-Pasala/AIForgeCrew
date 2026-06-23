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

_CAP = 8000


def _has(cwd: str, *names: str) -> bool:
    return any(os.path.exists(os.path.join(cwd, n)) for n in names)


def _node_pm(cwd: str) -> str:
    if os.path.exists(os.path.join(cwd, "pnpm-lock.yaml")):
        return "pnpm"
    if os.path.exists(os.path.join(cwd, "yarn.lock")):
        return "yarn"
    return "npm"


def _node_framework(cwd: str) -> str:
    try:
        pkg = json.loads(open(os.path.join(cwd, "package.json")).read())
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        for fw in ("next", "vite", "react-scripts", "react", "@angular/core", "vue"):
            if fw in deps:
                return fw
    except Exception:  # noqa: BLE001
        pass
    return "node"


def _has_tests(cwd: str, stacks: list[str]) -> bool:
    """Best-effort: does the project have a runnable test setup?"""
    if os.path.isdir(os.path.join(cwd, "src", "test")):   # maven/gradle
        return True
    if os.path.isdir(os.path.join(cwd, "tests")) or os.path.isdir(
            os.path.join(cwd, "test")):
        return True
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in {
            ".git", "node_modules", ".venv", "venv", "target", "build", "dist"}]
        for f in files:
            if (f.startswith("test_") and f.endswith(".py")) \
                    or f.endswith("_test.go") or f.endswith(".test.js") \
                    or f.endswith(".test.ts") or f.endswith(".spec.ts") \
                    or f.endswith("Test.java") or f.endswith("Tests.java"):
                return True
        if root.count(os.sep) - cwd.count(os.sep) >= 4:   # bound the walk
            dirs[:] = []
    # Node with a real "test" script (not the npm-init placeholder)?
    if any(s.startswith("node") for s in stacks):
        try:
            import json as _j
            pkg = _j.loads(open(os.path.join(cwd, "package.json")).read())
            t = ((pkg.get("scripts") or {}).get("test") or "")
            if t and "no test specified" not in t:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def detect(cwd: str) -> dict:
    """Detect the stack(s) present in ``cwd`` + whether tests exist."""
    stacks: list[str] = []
    if _has(cwd, "pom.xml"):
        stacks.append("maven")
    if _has(cwd, "build.gradle", "build.gradle.kts", "settings.gradle"):
        stacks.append("gradle")
    if _has(cwd, "package.json"):
        stacks.append(f"node:{_node_framework(cwd)}")
    if _has(cwd, "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"):
        stacks.append("python")
    if _has(cwd, "go.mod"):
        stacks.append("go")
    if _has(cwd, "Cargo.toml"):
        stacks.append("rust")
    return {"ok": True, "stacks": stacks, "cwd": cwd,
            "has_tests": _has_tests(cwd, stacks),
            "note": "no recognised project markers" if not stacks else ""}


def _plan(stack: str, action: str, cwd: str) -> tuple[list[str], list[str]]:
    """Return (tools_needed, commands) for one stack + action."""
    gradlew = os.path.exists(os.path.join(cwd, "gradlew"))
    if stack == "maven":
        tools = ["java", "mvn"]
        cmds = {
            "install": ["mvn -q -DskipTests dependency:resolve"],
            "build": ["mvn -q -DskipTests package"],
            "test": ["mvn -q test"],
            "run": ["mvn -q spring-boot:run"],
        }
        return tools, cmds.get(action, [])
    if stack == "gradle":
        g = "./gradlew" if gradlew else "gradle"
        tools = ["java"] + ([] if gradlew else ["gradle"])
        cmds = {
            "install": [f"{g} dependencies"],
            "build": [f"{g} build -x test"],
            "test": [f"{g} test"],
            "run": [f"{g} bootRun"],
        }
        return tools, cmds.get(action, [])
    if stack.startswith("node"):
        pm = _node_pm(cwd)
        fw = stack.split(":", 1)[1] if ":" in stack else "node"
        tools = ["node", pm]
        inst = {"npm": "npm install", "yarn": "yarn install", "pnpm": "pnpm install"}[pm]
        run_script = "dev" if fw in ("next", "vite", "react-scripts", "react", "vue") else "start"
        cmds = {
            "install": [inst],
            "build": [f"{pm} run build"],
            "test": [f"{pm} test --silent" if pm == "npm" else f"{pm} test"],
            "run": [f"{pm} run {run_script}"],
        }
        return tools, cmds.get(action, [])
    if stack == "python":
        tools = ["python3", "pip"]
        inst = ("pip install -r requirements.txt"
                if os.path.exists(os.path.join(cwd, "requirements.txt"))
                else "pip install -e .")
        entry = next((f for f in ("main.py", "app.py", "manage.py", "run.py")
                      if os.path.exists(os.path.join(cwd, f))), "main.py")
        cmds = {
            "install": [inst],
            "build": ["python -m compileall -q ."],
            "test": ["python -m pytest -q"],
            "run": [f"python {entry}"],
        }
        return tools, cmds.get(action, [])
    if stack == "go":
        return ["go"], {"install": ["go mod download"], "build": ["go build ./..."],
                        "test": ["go test ./..."], "run": ["go run ."]}.get(action, [])
    if stack == "rust":
        return ["cargo"], {"install": ["cargo fetch"], "build": ["cargo build"],
                           "test": ["cargo test"], "run": ["cargo run"]}.get(action, [])
    return [], []


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
    out, _ = proc.communicate()
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
