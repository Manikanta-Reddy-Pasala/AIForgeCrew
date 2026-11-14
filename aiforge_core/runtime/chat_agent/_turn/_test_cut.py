"""A pytest run that stopped before its summary line.

pytest always ends with a summary (``5 passed in 0.3s``, ``no tests ran``,
``1 error in 0.1s``). A run with no such line was ended from inside: code
under test called ``os._exit()`` / ``sys.exit()`` on a path a test reaches,
or the process died. With ``os._exit(0)`` the exit code is 0 and the output
just stops after the first file name — it reads as a pass.

click (core-mode bench): the model swapped ``sys.exit(e.exit_code)`` for
``_fast_exit()`` (``os._exit``) in ``main()``; every test calling ``main()``
ended pytest silently, the seed suite went 0/359 and the score sat at 6/37
for 15 minutes while each test run "passed".

``mark(name, args, result)`` adds a plain warning and ``tests_incomplete``
to such a result; a run whose output a pipe cut (``| head``, ``| grep``) or
the tool truncated is left alone: there the summary may simply be cut.
Off switch: ``AIFORGE_CHAT_TEST_CUT=0``.
"""
from __future__ import annotations

import os
import re

_PYTEST = re.compile(r"\bpytest\b|\bpy\.test\b")
_SUMMARY = re.compile(
    r"\b(?:\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|"
    r"warnings?|tests? collected|selected)|no tests ran)\b[^\n]*\bin \d+(?:\.\d+)?s\b"
    r"|\bno tests ran\b|\bINTERNALERROR\b|\bERROR: (?:file or directory not found|usage)"
    r"|^usage: ", re.M)
#: ``pytest -q`` has no header: a run of progress marks is its own output
_PROGRESS = re.compile(r"^[.FEsxX]+\s*(?:\[\s*\d+%\])?\s*$", re.M)
_CUTTERS = re.compile(r"\|\s*(?:head|grep|sed|awk|cut|less|more|wc)\b|--co\b|--collect-only\b"
                      r"|\s-h\b|--help\b|--version\b|--fixtures\b|--markers\b")

WARNING = ("pytest stopped before its summary line (no 'N passed/failed in Xs'): "
           "the run did not finish, so this is NOT a pass. Code under test ended the "
           "process — look for os._exit()/sys.exit()/exit() on a path a test reaches "
           "(a recent edit?). Run one test file with `-x -s -v` to see which test it is.")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_TEST_CUT", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _out(result: dict) -> str:
    return "\n".join(str(result.get(k) or "") for k in ("stdout", "stderr", "output"))


def is_cut(name, args, result) -> bool:
    if not _on() or not isinstance(result, dict) or result.get("running") \
            or result.get("background") or result.get("error"):
        return False
    args = args or {}
    cmd = str(args.get("cmd") or args.get("command") or "")
    if name == "run_tests":
        if str(result.get("language") or "python") != "python":
            return False
    elif not _PYTEST.search(cmd) or _CUTTERS.search(cmd):
        return False
    code = result.get("exit_code", result.get("code"))
    if code not in (0, None):
        return False                   # a non-zero exit already reads as a failure
    out = _out(result)
    if not out.strip() or result.get("truncated") or "truncated" in out[-200:].lower():
        return False
    if "test session starts" not in out and not re.search(r"^\S+\.py\b", out, re.M) \
            and not _PROGRESS.search(out):
        return False                   # not pytest's own output (a wrapper script)
    return not _SUMMARY.search(out)


def mark(name, args, result):
    """The result with the warning when the run was cut, else unchanged."""
    if not is_cut(name, args, result):
        return result
    return {**result, "ok": False, "tests_incomplete": True, "warning": WARNING}
