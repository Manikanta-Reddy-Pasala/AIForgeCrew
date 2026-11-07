"""After an edit lands in a chat turn: format the files, then hand the model
the errors in them (runtime/post_edit does the work; this is the turn's side —
what is said in the chat and in the conversation)."""
from __future__ import annotations

import re

from aiforge_core.runtime.tools.mutating import writes_files

from ._progress import _full, _named_paths

# "m.py:12:5: F821 …" / "a.ts(3,1): error …": the place, which moves when
# lines above change; the same error further down is not new.
_PLACE = re.compile(r"^.*?(?::\d+:\d+:|\(\d+,\d+\):)\s*")


def _quiet(fn, *args):
    """A formatter or checker never ends a turn."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


def edited_files(name, args, result, cwd) -> list:
    """(as named, absolute) for each file a landed write touched — resolved
    the way the progress bookkeeping does (_progress.note_write)."""
    if not writes_files(name, args if isinstance(args, dict) else {}):
        return []
    if isinstance(result, dict) and result.get("ok") is False:
        return []
    named = _named_paths(args, None) or _named_paths(None, result)
    return [(p, _full(p, cwd)) for p in dict.fromkeys(named)]


def post_edit_format(st, files, cwd):
    """Format the files just written with the project's formatter. When that
    changed one, the model's copy is stale: say so, so its next patch reads
    the file first. Runs BEFORE the write is hashed, so the progress state
    sees the file as it stays."""
    from aiforge_core.runtime import post_edit
    for shown, full in files:
        fmt = _quiet(post_edit.format_file, full, cwd)
        if fmt:
            yield {"type": "thought", "role": "system",
                   "text": f"Formatted {shown} with {fmt}"}
            st.convo.append({"role": "user", "content":
                f"[automated formatter — not the user] {fmt} reformatted {shown} "
                "after your edit. Read the lines again before you patch this file."})


def post_edit_diagnostics(st, files, cwd):
    """Errors (never style) in the files just written, handed to the model
    this step. The same errors (wherever the lines moved) are said once; a
    check that could not run says nothing and forgets nothing."""
    from aiforge_core.runtime import post_edit
    said = getattr(st, "post_edit_said", None)
    if said is None:
        said = st.post_edit_said = {}
    for shown, full in files:
        errs = _quiet(post_edit.diagnostics, full, cwd)
        if not errs:
            # the file is clean on its own: does its package still import?
            imp = _quiet(import_error, st, full)
            if imp:
                errs = [imp]
        if errs is None:
            continue
        if not errs:
            said.pop(full, None)
            (getattr(st, "open_errors", None) or {}).pop(full, None)
            continue
        key = sorted(_LINE_NO.sub(")", _PLACE.sub("", e)) for e in errs)
        opened = st.__dict__.setdefault("open_errors", {}) if hasattr(st, "__dict__") else {}
        new = _new_since_base(st, full, cwd, key)
        if new and (full not in opened or opened[full][2] != new):
            opened[full] = (shown, getattr(st, "step_n", 0) or 0, new)
        elif not new:
            opened.pop(full, None)
        if said.get(full) == key:
            continue
        said[full] = key
        yield {"type": "thought", "role": "system",
               "text": f"⚠ {len(errs)} error(s) in {shown} — fix them now: {errs[0][:160]}"}
        hints = _quiet(_where_defined, errs, full) or []
        st.convo.append({"role": "user", "content":
            "[automated error check — not the user] The file you just wrote has "
            f"these errors; fix them before continuing:\n" + "\n".join(errs)[:1500]
            + ("\n" + "\n".join(hints) if hints else "")})


_UNDEFINED = re.compile(r"F821 Undefined name [`'\"]?(\w+)")
_ASIDE = re.compile(r"(?:^|/)(?:tests?|docs?|examples?|benchmarks?|scripts|vendor|_vendor|"
                    r"\.worktrees|build|dist|site-packages)/")


def _module_of(path: str) -> str:
    """The dotted module a file is, from its package folders (``__init__.py``
    up the tree); a file outside a package is its own name."""
    import os
    parts = [os.path.splitext(os.path.basename(path))[0]]
    d = os.path.dirname(path)
    while os.path.isfile(os.path.join(d, "__init__.py")):
        parts.insert(0, os.path.basename(d))
        d = os.path.dirname(d)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _where_defined(errs, full) -> list:
    """For an undefined name the project defines (``class`` / ``def``) in one
    clear place elsewhere: where, and how to import it. click a3 got
    "Undefined name CompletionItem" in types.py seven times; the class lives
    in shell_completion.py, which imports types — the import has to sit
    inside the function that uses it. A name defined in several places, or
    only in tests/docs/vendored copies, gets no hint (a wrong one is worse)."""
    import os
    import subprocess
    names = list(dict.fromkeys(m.group(1) for e in errs if (m := _UNDEFINED.search(e))))[:5]
    if not names:
        return []
    full = os.path.realpath(full)
    top = subprocess.run(["git", "-C", os.path.dirname(full), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=4)
    if top.returncode != 0:
        return []
    root = os.path.realpath(top.stdout.strip())
    me = os.path.relpath(full, root).replace(os.sep, "/")
    alt = "|".join(names)
    hit = subprocess.run(
        ["git", "-C", root, "grep", "--untracked", "-z", "-n", "-E",
         rf"^(class|def|async def) ({alt})([^A-Za-z0-9_]|$)", "--", "*.py"],
        capture_output=True, text=True, timeout=4)
    found: dict = {}
    for row in hit.stdout.split("\n"):
        bits = row.split("\0")
        if len(bits) < 3 or bits[0] == me or _ASIDE.search(bits[0]):
            continue
        m = re.match(r"(?:class|def|async def) (\w+)", bits[2])
        if m and m.group(1) in names:
            found.setdefault(m.group(1), []).append(bits[0])
    my_top = me.split("/")[0]
    my_mod = _module_of(full).rsplit(".", 1)[-1]
    out = []
    for name in names:
        where = sorted(set(found.get(name) or []),
                       key=lambda p: (p.split("/")[0] != my_top, p.count("/"), p))
        if not where or len(where) > 2:
            continue                  # nowhere, or no one clear owner
        src = where[0]
        mod = _module_of(os.path.join(root, src))
        try:
            with open(os.path.join(root, src), encoding="utf-8", errors="replace") as fh:
                text = fh.read(200_000)
            cycle = bool(re.search(rf"^\s*(?:from\s+[\w.]*\s+import\s+[^\n]*\b{my_mod}\b|"
                                   rf"from\s+[\w.]*\b{my_mod}\s+import|import\s+[\w.]*\b{my_mod}\b)",
                                   text, re.M))
        except OSError:
            cycle = False
        how = ("that module imports this file, so put the import inside the function "
               "that uses it (a top-level import would be circular)."
               if cycle else "import it at the top of this file.")
        out.append(f"Hint: `{name}` is defined in {src} (module `{mod}`); {how}")
    return out


def _new_since_base(st, full, cwd, key) -> list:
    """The errors of ``key`` the file did not have at the commit the work
    began (the repo's own old ones are not this turn's to fix); [] when that
    cannot be told."""
    import os
    import subprocess
    import tempfile
    from aiforge_core.runtime import post_edit
    try:
        from ._contract_gate import work_base
        base = work_base(st)
        top = subprocess.run(["git", "-C", os.path.dirname(full), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=4)
        if not base or top.returncode != 0:
            return []
        rel = os.path.relpath(os.path.realpath(full), os.path.realpath(top.stdout.strip()))
        old = subprocess.run(["git", "-C", top.stdout.strip(), "show", f"{base}:{rel}"],
                             capture_output=True, text=True, timeout=4)
        if old.returncode != 0:
            return list(key)          # a file the turn created: every error is new
        with tempfile.TemporaryDirectory(prefix="aiforge-base-") as d:
            p = os.path.join(d, os.path.basename(full))
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(old.stdout)
            was = _quiet(post_edit.diagnostics, p, d)
        if was is None:
            return []
        before = {_PLACE.sub("", e).replace(p, "") for e in was}
        return [k for k in key if k.replace(full, "") not in before]
    except Exception:  # noqa: BLE001 — cannot tell: say nothing more
        return []


_IMPORT_TIMEOUT_S = 20
_TRACE_TAIL = re.compile(r"^([\w.]+(?:Error|Exception|Configured|Exit)):\s*(.+)$", re.M)
_TRACE_LINE = re.compile(r'File "([^"]+)", line (\d+)')
_SKIP_PATH = re.compile(r"(?:^|/)(?:site-packages|dist-packages|\.venv|venv|\.aiforge-venv)/")


def _package_root(full: str) -> "tuple[str, str] | None":
    """``(sys.path entry, dotted module)`` of a file inside a package (real
    paths); None for a loose script or a ``__main__``."""
    import os
    full = os.path.realpath(full)
    if os.path.basename(full) == "__main__.py":
        return None                       # importing it runs the program
    mod = _module_of(full)
    pkg_init = os.path.basename(full) == "__init__.py"
    if "." not in mod and not pkg_init:
        return None                       # a loose script: nothing to import
    root = os.path.dirname(full)          # the folder of the file
    if pkg_init:
        root = os.path.dirname(root)      # a package's __init__: its parent
    for _ in range(mod.count(".")):      # one folder up per dot: the path entry
        root = os.path.dirname(root)
    return root, mod


def _try_import(py: str, root: str, mod: str) -> "str | None":
    """The error importing ``mod`` from ``root`` with ``py``; None when it
    imports, cannot be told, or fails only on a package that is not the
    project's own (an optional or third-party dependency); ``"timeout"``."""
    import os
    import signal
    import subprocess
    env = dict(os.environ, PYTHONPATH=root + (os.pathsep + os.environ["PYTHONPATH"]
                                               if os.environ.get("PYTHONPATH") else ""))
    env.pop("PYTHONSAFEPATH", None)
    try:
        p = subprocess.Popen([py, "-c", f"import {mod}"], cwd=root, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError:
        return None
    try:
        _out, err = p.communicate(timeout=_IMPORT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)  # and anything it started
        except OSError:
            pass
        p.communicate()
        return "timeout"
    if p.returncode == 0:
        return None
    tail = _TRACE_TAIL.findall(err or "")
    if not tail:
        return None
    kind, msg = tail[-1]
    if kind.endswith("ModuleNotFoundError"):
        missing = re.search(r"No module named '([^']+)'", msg)
        top = mod.split(".")[0]
        if not missing or missing.group(1).split(".")[0] != top:
            return None                   # not the project's own module
        if mod == missing.group(1) or mod.startswith(missing.group(1) + "."):
            return None                   # the path guess was wrong, not the code
    where = [(f, n) for f, n in _TRACE_LINE.findall(err or "")
             if os.path.realpath(f).startswith(root + os.sep) and not _SKIP_PATH.search(f)]
    at = f" ({os.path.relpath(os.path.realpath(where[-1][0]), root)}:{where[-1][1]})" if where else ""
    return f"{kind}: {msg.strip()[:200]}{at}"


def _base_imports(st, root: str, mod: str, py: str) -> bool:
    """``mod`` imports on the commit the work began (a git worktree of it, no
    hooks); False when it does not or cannot be told."""
    import os
    import shutil
    import subprocess
    import tempfile
    try:
        from ._contract_gate import work_base
        base = work_base(st)
        top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if not base or not top:
            return False
        top = os.path.realpath(top)
        tmp = tempfile.mkdtemp(prefix="aiforge-import-")
        w = os.path.join(tmp, "b")
        try:
            if subprocess.run(["git", "-C", top, "-c", "core.hooksPath=/dev/null",
                               "worktree", "add", "--detach", w, base],
                              capture_output=True, timeout=60).returncode != 0:
                return False
            broot = os.path.join(w, os.path.relpath(root, top))
            if not os.path.isdir(broot):
                return False
            return _try_import(py, broot, mod) is None
        finally:
            subprocess.run(["git", "-C", top, "worktree", "remove", "--force", w],
                           capture_output=True, timeout=60)
            subprocess.run(["git", "-C", top, "worktree", "prune"], capture_output=True,
                           timeout=30)
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception:  # noqa: BLE001
        return False


def import_error(st, full) -> "str | None":
    """``import <module>`` of an edited package file fails now and did not on
    the commit the work began, run with the project's interpreter (the one the
    turn's own tests use; none known: no check). attrs n3 (0/18): a function
    called while _make.py was still loading, defined further down; the file
    linted clean, the package could not import, every test failed."""
    import os
    if not str(full).endswith(".py"):
        return None
    try:
        from ._outcomes import _project_python
        py = _project_python(st, getattr(st, "cwd", None) or os.path.dirname(full))
    except Exception:  # noqa: BLE001
        return None
    found = _package_root(full)
    if not py or not found:
        return None
    root, mod = found
    slow = st.__dict__.setdefault("import_slow", set()) if hasattr(st, "__dict__") else set()
    if (root, mod) in slow:
        return None                       # timed out once: not checked again
    err = _try_import(py, root, mod)
    if err == "timeout":
        slow.add((root, mod))
        return None
    if not err:
        return None
    seen = st.__dict__.setdefault("import_base_ok", {}) if hasattr(st, "__dict__") else {}
    if (root, mod) not in seen:
        seen[(root, mod)] = _base_imports(st, root, mod, py)
    if not seen[(root, mod)]:
        return None                       # broken at the base too, or unknown
    return (f"import {mod} fails: {err} — the package cannot be imported, so every "
            "test that uses it fails")


_LINE_NO = re.compile(r":\d+\)")


#: Steps an error the check named may stay in a file before it is said again,
#: louder (attrs n3: `_make_class_props` undefined in _make.py, said twice, then
#: 30 minutes of reads with the package unable to import — 0/18).
STALE_STEPS = 8


def stale_errors(st, cwd, n=None):
    """Once per file and error set: errors named STALE_STEPS steps ago that
    the file still has, said again as the thing to fix first."""
    from aiforge_core.runtime import post_edit
    opened = getattr(st, "open_errors", None) or {}
    if not opened or getattr(st, "goals_cut", False):
        return
    told = st.__dict__.setdefault("open_errors_told", set()) if hasattr(st, "__dict__") else set()
    now = n if n is not None else (getattr(st, "step_n", 0) or 0)
    for full, (shown, since, key) in list(opened.items()):
        if now - since < STALE_STEPS or (full, tuple(key)) in told:
            continue
        errs = _quiet(post_edit.diagnostics, full, cwd)
        if not errs:
            imp = _quiet(import_error, st, full)    # an import error has no lint line
            if imp:
                errs = [imp]
        if errs is None:
            continue
        if not errs:
            opened.pop(full, None)
            (getattr(st, "post_edit_said", None) or {}).pop(full, None)
            continue
        told.add((full, tuple(key)))
        yield {"type": "thought", "role": "system",
               "text": f"⚠ {shown} still has {len(errs)} error(s) from {now - since} steps ago — "
                       "fixing them first"}
        hints = _quiet(_where_defined, errs, full) or []
        from ._limits import _ride
        _ride(st, "[automated error check — not the user] " + shown + " still has the "
                  f"errors named {now - since} steps ago. Until they are fixed the module "
                  "cannot be imported or run, so every test that touches it fails. Fix "
                  "them now, before anything else — define the missing name, import it, "
                  "or remove the call:\n" + "\n".join(errs)[:1500]
                  + ("\n" + "\n".join(hints) if hints else ""))


__all__ = ["edited_files", "post_edit_diagnostics", "post_edit_format", "stale_errors"]
