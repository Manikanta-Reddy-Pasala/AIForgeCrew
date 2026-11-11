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


def note(st, name, args, text: str, cwd) -> str:
    """The note for the first test run that could not start for a missing
    module, else ""."""
    if not _on():
        return ""
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
