"""A test run that could not start because a dependency is missing: say so at
once, with the interpreter that ran it.

tomlkit (fresh-repo bench, most runs): the tests import ``yaml``; the agent's
``python3 -m pytest`` stopped at collection with ``ModuleNotFoundError: No
module named 'yaml'``. The agent read that as "1 error" among its own
failures, kept changing the parser, tried ``pip install`` into the system
python (blocked), made a .venv but went on running the tests with the
system python. The whole hour went by with no test result at all.

After a test run whose output names a missing module that is not one of the
repository's own packages, the agent is told once per module: the tests did
not run, which interpreter ran them, and how to put the module there
(``<that python> -m pip install X``; when that is refused, a venv in the
repo and the tests run with its python). Python only. Off switch:
``AIFORGE_CHAT_ENV_GUARD=0``.
"""
from __future__ import annotations

import os
import re

_MISSING = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '([\w.]+)'")
#: The run never got to the tests: pytest's collection errors.
_COLLECTION = re.compile(r"error(?:s)? during collection|ERROR collecting|"
                         r"Interrupted: \d+ errors? during collection|ImportError while importing",
                         re.I)
#: pip / import name differs.
_PIP_NAME = {"yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python", "sklearn": "scikit-learn",
             "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "dotenv": "python-dotenv",
             "jwt": "pyjwt", "Crypto": "pycryptodome", "google": "protobuf"}


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_ENV_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _own(top: str, mod: str) -> bool:
    """``mod`` is the repository's own package or module."""
    head = mod.split(".")[0]
    for base in (top, os.path.join(top, "src")):
        if os.path.isdir(os.path.join(base, head)) or os.path.isfile(os.path.join(base, head + ".py")):
            return True
    return False


def missing_modules(text: str, top: str) -> list:
    """Third-party modules a test run could not import while collecting."""
    if not text or not _COLLECTION.search(text):
        return []
    out = []
    for mod in _MISSING.findall(text):
        head = mod.split(".")[0]
        if head not in out and not _own(top, head):
            out.append(head)
    return out


#: The JDK running the build is older than the project's release/target.
_JDK_TOO_OLD = re.compile(
    r"release version (\d+) not supported|invalid (?:target|source) release:?\s*(\d+)|"
    r"class file (?:major )?version (\d+)|Unsupported class file major version (\d+)", re.I)
_JDK_GLOBS = ("/usr/lib/jvm/*", "~/.sdkman/candidates/java/*", "~/jdk*", "~/jdk*/*", "~/jdks/*",
              "~/.jdks/*", "/opt/*jdk*", "/opt/*jdk*/*", "/opt/java/*",
              "/Library/Java/JavaVirtualMachines/*/Contents/Home")


def _jdk_major(home: str) -> "int | None":
    try:
        with open(os.path.join(home, "release"), encoding="utf-8", errors="replace") as fh:
            m = re.search(r'JAVA_VERSION="(\d+)(?:\.(\d+))?', fh.read())
    except OSError:
        return None
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major   # "1.8" -> 8


def installed_jdks() -> list:
    """``[(major, home)]`` of the JDKs found in the usual places, newest first."""
    import glob
    seen, out = set(), []
    for pat in _JDK_GLOBS:
        for home in glob.glob(os.path.expanduser(pat)):
            real = os.path.realpath(home)
            if real in seen or not os.path.isfile(os.path.join(home, "bin", "java")):
                continue
            seen.add(real)
            major = _jdk_major(home)
            if major:
                out.append((major, home))
    return sorted(out, key=lambda t: -t[0])


def _jdk_note(st, text: str) -> str:
    """A build stopped because its JDK is too old: name the installed JDKs that
    are new enough. A real PosClientBackend job (needs 24, system java 21)
    never found the JDK 24 at ~/jdk24/... in a fresh run, never ran a test,
    and finished with a test file that did not compile (0/8)."""
    m = _JDK_TOO_OLD.search(text or "")
    if not m or getattr(st, "env_guard_jdk_said", False):
        return ""
    raw = int(next(g for g in m.groups() if g))
    need = raw - 44 if raw >= 45 else raw          # class-file major 68 -> Java 24
    st.env_guard_jdk_said = True
    fit = [(v, h) for v, h in installed_jdks() if v >= need][:4]
    if not fit:
        return ("[harness — not the user] The build did not run: it needs JDK "
                f"{need} or newer and none was found in the usual places (/usr/lib/jvm, "
                "~/.sdkman, ~/jdk*, /opt). Find or install one before changing code; until a "
                "test run works, nothing you change is checked.")
    lines = "\n".join(f"  JAVA_HOME={h} PATH={h}/bin:$PATH <your mvn/gradle command>   (Java {v})"
                      for v, h in fit)
    return ("[harness — not the user] The build did not run: the java on PATH is older than "
            f"the JDK {need} the project needs. These installed JDKs are new enough — prefix "
            f"every build/test command with one:\n{lines}\nUntil a test run works, nothing "
            "you change is checked; do not edit the build files to lower the version.")


def note(st, name, args, text: str, cwd) -> str:
    """The note for the first test run that could not start for a missing
    module, or because its JDK is too old; else ""."""
    if not _on():
        return ""
    jdk = _jdk_note(st, text)
    if jdk:
        return jdk
    top = str(cwd or ".")
    try:
        import subprocess
        r = subprocess.run(["git", "-C", top, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            top = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    said = getattr(st, "env_guard_said", None)
    if said is None:
        said = st.env_guard_said = set()
    mods = [m for m in missing_modules(text, top) if m not in said]
    if not mods:
        return ""
    said.update(mods)
    py = ""
    try:
        from aiforge_core.runtime.shell_writes import command_of
        from ._done_watch import _interpreter
        py = _interpreter(command_of(args) or "") or ""
    except Exception:  # noqa: BLE001
        py = ""
    py = py or "python3"
    pkgs = " ".join(_PIP_NAME.get(m, m) for m in mods)
    names = ", ".join(f"`{m}`" for m in mods)
    return ("[harness — not the user] These tests did not run: collecting them failed because "
            f"{names} is not installed for the interpreter that ran them ({py}). Fix that "
            f"before changing any code: `{py} -m pip install {pkgs}`; if pip refuses "
            "(an externally managed system python), make a venv in the repo "
            f"(`{py} -m venv .venv && .venv/bin/pip install {pkgs}`) and run the tests with "
            "`.venv/bin/python -m pytest`. If only test files unrelated to your change need "
            "it, run the test files that cover your change instead "
            "(`pytest tests/test_x.py`, or `--ignore` the ones that import it). Until the "
            "tests run, a failure count says nothing about your change.")


__all__ = ["missing_modules", "note"]
