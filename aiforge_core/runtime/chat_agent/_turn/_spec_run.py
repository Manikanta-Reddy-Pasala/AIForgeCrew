"""Before the answer: run the request's own call examples, and import what the
turn changed, the way the turn's tests run.

Scoreboard r1 (11-08): the model answered "all tests pass" while the code
failed cases the request spelled out - ``split_top_level`` on nested
brackets, a ``Duration`` that rejected ``"90"`` - and once while the
package it edited no longer imported (``from attr._next_gen import diff``).
The other finish checks read the code or ask the model; this one runs it.

Examples are the request's backticked calls with a result:
``` `f("a, b")` -> `["a", "b"]` ``` (also ``returns``, ``gives``, ``==``, or
both in one span). Arguments and the result must be Python literals; the
function is found by its module-level ``def`` in the repository's non-test
code; a call stated twice keeps its last result, and one in a sentence about
the current or a wrong behaviour is skipped. The top-level package of each
changed file is imported (``import pkg`` - never a script's own code).
Run with the interpreter, folder and environment of the turn's last test
command, in its own process group, 30 s. Failures send the answer back
once. Python only.
``AIFORGE_CHAT_SPEC_RUN=0`` turns it off.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tempfile

_TIMEOUT_S = 30
_MAX_EXAMPLES = 20
_TWO = re.compile(r"`([A-Za-z_][\w.]*\([^`]*\))`\s*(?:->|=>|==|returns?|gives?|yields?)\s*`([^`]+)`")
_ONE = re.compile(r"`([A-Za-z_][\w.]*\([^`]*?\))\s*(?:->|=>|==)\s*([^`]+)`")

_RUNNER = r'''
import importlib, json, sys, traceback
spec = json.loads(sys.argv[1]); out = []
for mod in spec["imports"]:
    try:
        importlib.import_module(mod)
    except BaseException as exc:          # SystemExit from a CLI too
        out.append({"kind": "import", "what": mod, "error": f"{type(exc).__name__}: {exc}"})
for ex in spec["examples"]:
    try:
        fn = getattr(importlib.import_module(ex["module"]), ex["name"])
        got = fn(*ex["args"], **ex["kwargs"])
        try:
            same = bool(got == ex["want"]) or (isinstance(got, tuple) and list(got) == ex["want"])
        except Exception:
            same = True                         # not comparable here: say nothing
        if not same:
            out.append({"kind": "example", "what": ex["call"], "want": repr(ex["want"]),
                        "got": repr(got)[:300]})
    except BaseException as exc:
        out.append({"kind": "example", "what": ex["call"], "want": repr(ex["want"]),
                    "got": f"raised {type(exc).__name__}: {str(exc)[:200]}"})
print(json.dumps(out))
'''


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_SPEC_RUN", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _json_safe(value) -> bool:
    """Survives the hand-off to the runner unchanged (JSON keeps lists, str
    dict keys, numbers, str, bool, None; not tuples, sets, bytes)."""
    try:
        return json.loads(json.dumps(value)) == value and not isinstance(value, tuple)
    except (TypeError, ValueError):
        return False


def examples(text: str) -> list:
    """``[{call, name, args, kwargs, want}]`` from the request's call examples.
    A call stated twice keeps its LAST result; one in a sentence about the
    current or a wrong behaviour is not an example."""
    from ._example_gate import _NOT_EXPECTED
    text = text or ""
    found = {}
    for m in sorted(list(_TWO.finditer(text)) + list(_ONE.finditer(text)), key=lambda m: m.start()):
        call, want_src = m.group(1).strip(), m.group(2).strip()
        start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        if _NOT_EXPECTED.search(text[start:m.start()]):
            found.pop(call, None)
            continue
        try:
            node = ast.parse(call, mode="eval").body
            want = ast.literal_eval(want_src)
            if not isinstance(node, ast.Call):
                continue
            args = [ast.literal_eval(a) for a in node.args]
            kwargs = {k.arg: ast.literal_eval(k.value) for k in node.keywords if k.arg}
        except (SyntaxError, ValueError, TypeError):
            continue                          # not literals: not runnable here
        func = node.func
        if isinstance(func, ast.Attribute):
            continue                          # X.method(...): not a module-level function
        name = getattr(func, "id", "")
        if not name or not all(_json_safe(v) for v in (want, args, kwargs)):
            continue
        found[call] = {"call": call, "name": name, "args": args, "kwargs": kwargs, "want": want}
    return list(found.values())[:_MAX_EXAMPLES]


def _module_of(top: str, path: str) -> str:
    from ._post_edit import _module_of as mod
    return mod(path)


def _defs(top: str, names: set) -> dict:
    """``{name: module}`` for module-level ``def name(`` in non-test code."""
    found = {}
    pat = re.compile(r"^def (%s)\(" % "|".join(map(re.escape, names)), re.M) if names else None
    if not pat:
        return found
    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in (
            "tests", "test", "node_modules", "venv", ".venv", "build", "dist", "docs",
            "vendor", "_vendor", "third_party", "site-packages", "migrations", "scripts")]
        for f in files:
            if not f.endswith(".py") or f.startswith("test_"):
                continue
            p = os.path.join(root, f)
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    for m in pat.finditer(fh.read()):
                        found.setdefault(m.group(1), _module_of(top, p))
            except OSError:
                continue
    return found


def _changed_modules(cwd, base: str) -> list:
    from ._trace_hint import _git
    top = _git(cwd, "rev-parse", "--show-toplevel").strip() or str(cwd)
    # only the files of the change (tracked diff), never untracked scratch scripts
    names = _git(top, "diff", "--name-only", base, "--", "*.py").split()
    mods = []
    for rel in names:
        if re.search(r"(^|/)(tests?|testing|docs?|examples?)/|(^|/)test_[^/]*\.py$|_test\.py$|"
                     r"conftest\.py$|setup\.py$", rel):
            continue
        full = os.path.join(top, rel)
        if not os.path.isfile(full) or not os.path.isfile(
                os.path.join(os.path.dirname(full), "__init__.py")):
            continue                          # a script outside a package: not imported
        pkg = _module_of(top, full).split(".")[0]
        # the top-level package only: what `import pkg` runs (its __init__),
        # never a module's own top-level code (a CLI, a migration, a server)
        if pkg and pkg not in mods:
            mods.append(pkg)
    return mods[:10]


def run(st, cwd) -> list:
    """The failures (``[{kind, what, ...}]``); [] when all ran fine or nothing
    could be run."""
    last = getattr(st, "last_test_cmd", None)
    if not last:
        return []
    from aiforge_core.runtime.shell_writes import command_of
    from ._contract_gate import work_base
    from ._done_watch import _interpreter
    from ._trace_hint import _run_env
    cmd = command_of(last[1]) or ""
    python = _interpreter(cmd)
    base = work_base(st)
    if not python or not os.path.isabs(python) or not os.path.exists(python) or not base:
        return []
    exs = examples(str(getattr(st, "goal", "") or ""))
    here, env = _run_env(cmd, cwd)
    found = _defs(here, {e["name"] for e in exs}) if exs else {}
    runnable = [{**e, "module": found[e["name"]]} for e in exs if e["name"] in found]
    imports = _changed_modules(cwd, base)
    if not runnable and not imports:
        return []
    with tempfile.TemporaryDirectory(prefix="af-specrun-") as tmp:
        script = os.path.join(tmp, "r.py")
        with open(script, "w") as fh:
            fh.write(_RUNNER)
        env = {**env, "PYTHONPATH": os.pathsep.join(
            p for p in (here, env.get("PYTHONPATH", "")) if p)}
        try:
            payload = json.dumps({"imports": imports, "examples": runnable})
            proc = subprocess.Popen([python, script, payload], cwd=here, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        except (OSError, TypeError, ValueError):
            return []
        try:
            out, _ = proc.communicate(timeout=_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)      # a server an import started too
            except OSError:
                proc.kill()
            proc.communicate()
            st.spec_run_done = True
            # A run that never ends is the worst result, not "nothing ran":
            # a loop that never stops on one of the request's inputs.
            return [{"kind": "hang", "what": ", ".join(
                [e["call"] for e in runnable][:4] + [f"import {m}" for m in imports][:2])}]
    st.spec_run_done = True                   # used up only once something ran
    try:
        return json.loads((out or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return []


def message(fails: list) -> str:
    rows = []
    for f in fails[:12]:
        if f["kind"] == "hang":
            rows.append(f"- running {f['what']} did not finish in {_TIMEOUT_S} s: the code "
                        "probably loops forever on one of these inputs (a `while` that "
                        "never moves on, a retry with no end)")
        elif f["kind"] == "import":
            rows.append(f"- `import {f['what']}` fails: {f['error'][:200]}")
        else:
            rows.append(f"- `{f['what']}` should give `{f['want']}` but gives {f['got']}")
    return ("[harness — not the user] Before your answer, the request's own examples were "
            "run against your code, and the modules you changed were imported, the way your "
            "tests run:\n" + "\n".join(rows)
            + "\nThe request's examples are its tests. Fix the code so each one gives the "
              "result the request states (add each as a test), then run the tests.")


def gate(st, step, builder, strict_finish):
    """Once per turn: ``"continue"`` with the failures, else None."""
    if not _on() or builder or strict_finish or getattr(st, "spec_run_done", False) \
            or getattr(st, "readonly_mode", False) or getattr(st, "plan_mode", False) \
            or not getattr(st, "edits_made", 0):
        return None
    try:
        fails = run(st, getattr(st, "cwd", None) or ".")
    except Exception:  # noqa: BLE001 — a check never breaks the finish
        return None
    if not fails:
        return None
    yield {"type": "thought", "role": "system",
           "text": f"✗ {len(fails)} of the request's examples / imports fail when run — sending back"}
    if step.get("text"):
        yield {"type": "thought", "text": step["text"]}
    st.convo.append({"role": "user", "content": message(fails)})
    return "continue"
