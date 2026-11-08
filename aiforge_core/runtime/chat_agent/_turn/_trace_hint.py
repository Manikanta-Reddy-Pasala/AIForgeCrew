"""Which of the lines this turn added actually ran, when the same tests keep
failing after a fix.

sqlglot t1 (11-08): ``_parse_create`` consumed the TRIGGER token, then the
new ``_parse_create_trigger`` matched TRIGGER again and fell back to
``exp.Command`` on its second line. The model ran the tests 22 times and
never saw why; asked outright at that point it named the cause 1 time in 4.
Told which of its new lines ran ("returned early at ``if not
self._match(TokenType.TRIGGER)``; none of the later lines ran") it named it
2 in 4 and read the right lines in the other two.

When a pytest run fails with the same failing tests as the run before it,
with an edit in between, the failing tests (up to 3) are run again with a
line tracer limited to the Python files changed since the work began. The
note says, for each block of added lines, how many ran, the last that ran,
and the ones that never ran. Python + pytest only; at most ``_MAX_PER_TURN``
attempts a turn; 120 s, its whole process group killed on timeout; never for
a failing run slower than 30 s, a parallel (-n) or a background suite.
``AIFORGE_CHAT_TRACE_HINT=0`` turns it off.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

_MAX_PER_TURN = 3
_MAX_TESTS = 3
_TIMEOUT_S = 120
#: A failing run slower than this is not traced (the trace would take minutes).
_SLOW_S = 30

_TRACER = r'''
import json, os, sys, threading
targets = {os.path.realpath(p) for p in json.loads(sys.argv[1])}
ids, out = json.loads(sys.argv[2]), sys.argv[3]
hits = {}
real = {}

def _local(frame, event, arg):
    if event == "line":
        hits.setdefault(real[frame.f_code.co_filename], set()).add(frame.f_lineno)
    return _local

def _global(frame, event, arg):
    f = frame.f_code.co_filename
    r = real.get(f)
    if r is None:
        r = real[f] = os.path.realpath(f)
    if r in targets:
        hits.setdefault(r, set()).add(frame.f_lineno)
        return _local
    return None

sys.path.insert(0, os.getcwd())     # as `python -m pytest` run from here does
sys.settrace(_global)
threading.settrace(_global)
import pytest
rc = -1
try:
    rc = int(pytest.main(ids + ["-q", "-p", "no:cacheprovider"]))
finally:
    sys.settrace(None)
    with open(out, "w") as fh:
        json.dump({"rc": rc, "hits": {k: sorted(v) for k, v in hits.items()}}, fh)
'''


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_TRACE_HINT", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _git(cwd, *args) -> str:
    r = subprocess.run(["git", "--no-optional-locks", "-C", str(cwd), *args],
                       capture_output=True, text=True, timeout=20)
    return r.stdout if r.returncode == 0 else ""


def added_blocks(cwd, base: str) -> dict:
    """``{abs path: [(first, last), ...]}``: runs of lines added since
    ``base`` in changed, non-test Python files (new files whole)."""
    out = {}
    top = _git(cwd, "rev-parse", "--show-toplevel").strip() or str(cwd)
    names = _git(top, "diff", "--name-only", base, "--", "*.py").split()
    names += _git(top, "ls-files", "--others", "--exclude-standard", "--full-name",
                  "--", "*.py").split()
    cwd = top                                 # every name above is relative to the top
    for rel in names:
        if re.search(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$|conftest\.py$", rel):
            continue
        full = os.path.join(str(cwd), rel)
        if not os.path.isfile(full):
            continue
        diff = _git(cwd, "diff", "-U0", base, "--", rel)
        blocks = []
        if not diff:                          # untracked: the whole file is new
            with open(full, encoding="utf-8", errors="replace") as fh:
                n = sum(1 for _ in fh)
            blocks = [(1, n)] if n else []
        for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff, re.M):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            if count:
                blocks.append((start, start + count - 1))
        if blocks:
            out[os.path.realpath(full)] = blocks
    return out


def _code_lines(path: str) -> list:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()


def _runnable(line: str) -> bool:
    """A line whose running says something about the call: not blank, a
    comment, a closing bracket, a decorator, or a ``def``/``class`` line (those
    run when the module is imported, whether or not the code is ever called)."""
    s = line.strip()
    return bool(s) and not s.startswith(("#", '"""', "'''", ")", "]", "}", "else:",
                                         "try:", "finally:", "@", "def ", "async def ",
                                         "class "))


def _stmt_lines(path: str) -> "set | None":
    """Lines where a statement starts (not a def/class line, not a
    docstring): the lines a line trace can see run. None when it does not parse."""
    import ast
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (SyntaxError, ValueError, OSError):
        return None
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
                and isinstance(node.value.value, str):
            continue                          # a docstring
        out.add(node.lineno)
    return out


def report(cwd, blocks: dict, hits: dict) -> str:
    """The note: per block of added lines, what ran and what never did."""
    rows = []
    for path, spans in blocks.items():
        src = _code_lines(path)
        stmts = _stmt_lines(path)
        ran = set(hits.get(path) or [])
        rel = os.path.relpath(path, os.path.realpath(str(cwd)))
        for a, b in spans:
            code = [i for i in range(a, b + 1) if i <= len(src)
                    and (i in stmts if stmts is not None else _runnable(src[i - 1]))]
            if not code:
                continue
            done = [i for i in code if i in ran]
            if not done:
                rows.append(f"- {rel}:{a}-{b}: none of these added lines ran")
                continue
            if len(done) == len(code):
                continue                      # all of it ran: nothing to point at
            last = max(done)
            never = [i for i in code if i not in ran and i > last]
            row = (f"- {rel}:{a}-{b}: {len(done)} of {len(code)} added lines ran; the last "
                   f"that ran is {last}: `{src[last - 1].strip()[:120]}`")
            if never:
                row += f"; lines {never[0]}-{never[-1]} after it never ran"
            rows.append(row)
    if not rows:
        return ""
    return ("[harness — not the user] The same tests failed again after your change, so "
            "they were run once more with a line trace of the code you added this turn:\n"
            + "\n".join(rows[:12])
            + "\nCode that never ran cannot be what fails: look at why execution stops "
              "or turns away at the last line that ran.")


def _run_env(cmd: str, cwd) -> "tuple[str, dict]":
    """The folder and environment the model's test command ran with: a leading
    ``cd DIR &&`` and ``VAR=value`` words before the interpreter."""
    import shlex
    here = str(cwd)
    m = re.match(r"\s*cd\s+(\S+)\s*&&\s*", cmd or "")
    if m:
        here = os.path.normpath(os.path.join(here, os.path.expanduser(m.group(1))))
        cmd = cmd[m.end():]
    env = dict(os.environ)
    try:
        for w in shlex.split(cmd):
            mm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", w)
            if not mm:
                break
            env[mm.group(1)] = mm.group(2)
    except ValueError:
        pass
    return here, env


def trace(cwd, python: str, ids: list, blocks: dict, cmd: str = "", stopped=None) -> dict:
    """``{abs path: [lines run]}`` for ``blocks``' files while ``ids`` run."""
    here, env = _run_env(cmd, cwd)
    with tempfile.TemporaryDirectory(prefix="af-trace-") as tmp:
        script, out = os.path.join(tmp, "t.py"), os.path.join(tmp, "hits.json")
        with open(script, "w") as fh:
            fh.write(_TRACER)
        try:
            proc = subprocess.Popen([python, script, json.dumps(list(blocks)), json.dumps(ids),
                                     out], cwd=here, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except OSError:
            return {}
        import signal
        import time
        t0 = time.monotonic()
        while proc.poll() is None:
            if time.monotonic() - t0 > _TIMEOUT_S or (stopped and stopped()):
                # the whole group: servers a fixture started must not outlive it
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.wait()
                return {}
            time.sleep(0.5)
        try:
            with open(out) as fh:
                got = json.load(fh)
        except (OSError, ValueError):
            return {}
        # 2-5: interrupted, usage error, no tests collected - it did not run them
        if not isinstance(got, dict) or got.get("rc") not in (0, 1):
            return {}
        return got.get("hits") or {}


def after_test(st, name, args, result, cwd) -> str:
    """Run after a test step. The note to send, or ""."""
    if not _on() or getattr(st, "readonly_mode", False) or getattr(st, "plan_mode", False) \
            or getattr(st, "builder", ""):
        return ""
    try:
        from aiforge_core.runtime import docker_sandbox
        if docker_sandbox.is_enabled():
            return ""                         # the tests run in the sandbox; this would not
    except Exception:  # noqa: BLE001
        pass
    if not isinstance(result, dict) or result.get("ok") is not False:
        if isinstance(result, dict) and result.get("ok") is True:
            st.trace_prev = None              # green: start over
        return ""
    try:
        from aiforge_core.runtime.failure_signature import result_text
        from aiforge_core.runtime.shell_writes import command_of
        from ._done_watch import _interpreter, failing_ids
        got = failing_ids(result_text(result))
        cmd = command_of(args) or ""
    except Exception:  # noqa: BLE001
        return ""
    if not got or "pytest" not in cmd:
        return ""
    # not for a slow, parallel or background suite: a trace runs 3-10x slower
    # and must not hold the step (or a port a long job uses) for minutes
    took = re.findall(r"\bin (\d+(?:\.\d+)?)s\b", result_text(result))
    if (took and float(took[-1]) > _SLOW_S) or re.search(r"(?:^|\s)(?:-n|--numprocesses)\b", cmd) \
            or (isinstance(args, dict) and args.get("background")):
        return ""
    ids = sorted(got[0] | got[1])
    if not ids:
        return ""
    edits = getattr(st, "edits_made", 0)
    prev = getattr(st, "trace_prev", None)
    st.trace_prev = (tuple(ids), edits)
    if not prev or prev[0] != tuple(ids) or prev[1] == edits:
        return ""                             # first failure, new failures, or no fix between
    shown = st.__dict__.setdefault("trace_shown", 0) if hasattr(st, "__dict__") else 0
    if shown >= _MAX_PER_TURN:
        return ""
    try:
        from ._contract_gate import work_base
        base = work_base(st)
        python = _interpreter(cmd)
        if not base or not python or not os.path.isabs(python) or not os.path.exists(python):
            return ""                         # only the interpreter the run named, by path
        blocks = added_blocks(cwd, base)
        if not blocks:
            return ""
        st.trace_shown = shown + 1            # an attempt counts, a timeout too
        sid = getattr(st, "session_id", None)

        def _stopped():
            try:
                from aiforge_core.runtime import chat_cancel
                return sid is not None and chat_cancel.is_cancelled(sid)
            except Exception:  # noqa: BLE001
                return False
        hits = trace(cwd, python, [i for i in ids if "::" in i][:_MAX_TESTS] or ids[:1],
                     blocks, cmd, _stopped)
        # no line of the changed files ran at all: the trace itself did not
        # reach the code (an import it could not make) - say nothing
        note = report(cwd, blocks, hits) if hits else ""
    except Exception:  # noqa: BLE001 — a hint never breaks the step
        return ""
    return note
