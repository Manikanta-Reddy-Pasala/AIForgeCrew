"""``ensure_runtime`` — self-provision missing language runtimes / build tools.

When an agent (Doer in the team pipeline, or the conversational chat
agent) tries to build/run a project and the toolchain isn't installed
(``java: command not found``, no ``mvn`` / ``python`` / ``node``), it
calls this to install the missing tools, verify them, and continue its
loop — so it can actually finish the job instead of giving up.

Deterministic (no LLM): checks ``which`` for each requested tool, maps it
to a package for the detected OS package manager (apt / brew / apk /
dnf / yum), installs the missing ones, then re-verifies with
``--version``. Returns a structured per-tool report.

Guard rails:
* ``AIFORGE_ALLOW_INSTALL=0`` disables installs entirely (report only).
* Installs are additive (never removes anything). This module never
  deletes — destructive ops are out of scope by design.
"""
from __future__ import annotations

import os
import shutil
import subprocess

# tool/command name → package name, per package manager.
_APT = {
    "java": "default-jdk", "javac": "default-jdk", "jar": "default-jdk",
    "mvn": "maven", "maven": "maven", "gradle": "gradle",
    "node": "nodejs", "npm": "npm", "npx": "npm", "yarn": "yarn",
    "python": "python3", "python3": "python3", "pip": "python3-pip",
    "pip3": "python3-pip", "venv": "python3-venv",
    "go": "golang", "dotnet": "dotnet-sdk-8.0",
    "rustc": "rustc", "cargo": "cargo",
    "git": "git", "make": "make", "gcc": "gcc", "g++": "g++",
    "curl": "curl", "unzip": "unzip", "ruby": "ruby", "php": "php",
}
_BREW = {
    "java": "openjdk", "javac": "openjdk", "jar": "openjdk",
    "mvn": "maven", "maven": "maven", "gradle": "gradle",
    "node": "node", "npm": "node", "npx": "node", "yarn": "yarn",
    "python": "python", "python3": "python", "pip": "python", "pip3": "python",
    "go": "go", "dotnet": "dotnet", "rustc": "rust", "cargo": "rust",
    "git": "git", "make": "make", "gcc": "gcc", "curl": "curl",
    "ruby": "ruby", "php": "php", "unzip": "unzip",
}
_VERSION_FLAG = {  # most tools take --version; a few differ
    "java": "-version", "javac": "-version", "go": "version",
}


def _allow_install() -> bool:
    return os.environ.get("AIFORGE_ALLOW_INSTALL", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _pkg_manager() -> str | None:
    for mgr in ("apt-get", "brew", "apk", "dnf", "yum"):
        if shutil.which(mgr):
            return mgr
    return None


def _sudo_prefix(mgr: str) -> list[str]:
    # brew refuses root; system managers need root unless we already are.
    if mgr == "brew":
        return []
    if os.geteuid() == 0 if hasattr(os, "geteuid") else False:
        return []
    return ["sudo", "-n"] if shutil.which("sudo") else []


def _sudo_install_allowed() -> bool:
    """A privileged (sudo) install is ungated here (it doesn't pass through the
    command_risk approval gate that a hand-typed ``sudo apt-get install`` would).
    So when the cautious-deploy gate is on (default), require an explicit
    AIFORGE_ALLOW_SUDO_INSTALL=1 opt-in before auto-running sudo."""
    cautious = os.environ.get("AIFORGE_RISK_ASK_CAUTION", "1").strip().lower() \
        not in ("0", "false", "no", "off")
    if not cautious:
        return True
    return os.environ.get("AIFORGE_ALLOW_SUDO_INSTALL", "").strip().lower() in (
        "1", "true", "yes", "on")


def _install_cmds(mgr: str, pkg: str) -> list[list[str]]:
    pre = _sudo_prefix(mgr)
    if mgr == "apt-get":
        return [pre + ["apt-get", "update"],
                pre + ["apt-get", "install", "-y", pkg]]
    if mgr == "brew":
        return [["brew", "install", pkg]]
    if mgr == "apk":
        return [pre + ["apk", "add", "--no-cache", pkg]]
    if mgr in ("dnf", "yum"):
        return [pre + [mgr, "install", "-y", pkg]]
    return []


def _version_of(tool: str) -> str | None:
    flag = _VERSION_FLAG.get(tool, "--version")
    try:
        p = subprocess.run([tool, flag], capture_output=True, text=True,
                           timeout=20)
        out = (p.stdout or "") + (p.stderr or "")  # java prints to stderr
        return out.strip().splitlines()[0][:120] if out.strip() else "installed"
    except Exception:  # noqa: BLE001
        return None


def ensure_runtime(tools: list[str]) -> dict:
    """Ensure each named runtime / build tool is installed; install if not.

    Use BEFORE building or running a project, or right after a command
    fails with "command not found", so the build can proceed. Pass the
    executables you need, e.g. ["java", "mvn"] for a Maven project,
    ["python3", "pip"] for Python, ["node", "npm"] for Node.

    Args:
      tools: executable names to ensure are on PATH.

    Returns: {"ok": bool, "results": {tool: {present, installed_now,
      version, error}}, "package_manager": str|None}. ``ok`` is True only
      when every requested tool is present afterwards.
    """
    if isinstance(tools, str):
        tools = [tools]
    tools = [t.strip() for t in (tools or []) if t and t.strip()]
    if not tools:
        return {"ok": True, "results": {}, "package_manager": _pkg_manager()}

    mgr = _pkg_manager()
    results: dict[str, dict] = {}
    overall_ok = True

    for tool in tools:
        if shutil.which(tool):
            results[tool] = {"present": True, "installed_now": False,
                             "version": _version_of(tool), "error": None}
            continue
        # Missing — try to install.
        if not _allow_install():
            results[tool] = {"present": False, "installed_now": False,
                             "version": None,
                             "error": "missing and AIFORGE_ALLOW_INSTALL=0"}
            overall_ok = False
            continue
        if mgr is None:
            results[tool] = {"present": False, "installed_now": False,
                             "version": None,
                             "error": "no supported package manager found"}
            overall_ok = False
            continue
        pkg_map = _BREW if mgr == "brew" else _APT
        pkg = pkg_map.get(tool, tool)
        install_err = None
        for cmd in _install_cmds(mgr, pkg):
            if cmd[:1] == ["sudo"] and not _sudo_install_allowed():
                install_err = ("privileged install blocked — set "
                               "AIFORGE_ALLOW_SUDO_INSTALL=1 to allow sudo installs")
                break
            try:
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=900)
                if p.returncode != 0:
                    install_err = (p.stderr or p.stdout or "").strip()[-300:]
            except Exception as exc:  # noqa: BLE001
                install_err = str(exc)[:300]
        present = shutil.which(tool) is not None
        results[tool] = {
            "present": present, "installed_now": present,
            "version": _version_of(tool) if present else None,
            "error": None if present else (
                install_err or f"install via {mgr} did not put {tool} on PATH"),
        }
        if not present:
            overall_ok = False

    return {"ok": overall_ok, "results": results, "package_manager": mgr}
