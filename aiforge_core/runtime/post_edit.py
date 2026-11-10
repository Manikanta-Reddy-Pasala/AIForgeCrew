"""After an edit lands: format the file, then check it for real errors.

The post-edit syntax check (chat_agent/_context/_verify) only catches a file
that does not parse. An undefined name, a wrong import, a type error — the
model learned of those at the end, when the tests failed, with many edits
in between. Here the file just written is checked for ERRORS only (never
style) and the model gets them on its next step.

Before that, the file is formatted — but only with a formatter the project
itself uses (its config is there), so a repo that does not use ruff or
prettier never gets a whole-file reformat in its diff (ruff keeps the
project's exclude list). gofmt is the language standard and always runs,
but never in vendor/. Only files inside the chat's folder are formatted.

Both are best-effort: a missing tool, a timeout or a crash changes nothing.
Off switches: ``AIFORGE_CHAT_POST_EDIT_FORMAT=0``,
``AIFORGE_CHAT_POST_EDIT_DIAGNOSTICS=0``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time

log = logging.getLogger("aiforge.post_edit")

_TIMEOUT_S = 30
#: A tsc run longer than this is not run on that repo again.
_TSC_SLOW_S = 10
#: Lines of diagnostics handed to the model per edit.
MAX_LINES = 12
# A tsc that timed out on a repo is not run there again (it would cost every edit).
_SLOW_TSC: set = set()


def _on(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in ("0", "false", "no", "off")


def _run(cmd: list, cwd: str, timeout: int = _TIMEOUT_S):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("post-edit %s failed: %s", cmd[0], exc)
        return None


def _up_to(path: str, top: str):
    """The file's folder and each parent up to ``top`` (the chat's cwd). A
    file outside ``top`` gets its own folder only: a config found higher up
    (the home folder) is not this project's."""
    # realpath both: the edited path comes resolved (_progress._full), and a
    # chat folder behind a symlink (/tmp on macOS) must still match.
    d = os.path.dirname(os.path.realpath(path))
    top = os.path.realpath(top)
    yield d
    while d != top and d.startswith(top + os.sep):
        d = os.path.dirname(d)
        yield d


def _find(path: str, top: str, names) -> "str | None":
    for d in _up_to(path, top):
        for n in names:
            if os.path.exists(os.path.join(d, n)):
                return d
    return None


def _ruff() -> "list | None":
    exe = shutil.which("ruff")
    if exe:
        return [exe]
    try:
        import ruff  # noqa: F401  (the app's own venv carries it)
        return [sys.executable, "-m", "ruff"]
    except ImportError:
        return None


def _uses_ruff(path: str, top: str) -> bool:
    if _find(path, top, ("ruff.toml", ".ruff.toml")):
        return True
    d = _find(path, top, ("pyproject.toml",))
    if not d:
        return False
    try:
        with open(os.path.join(d, "pyproject.toml"), encoding="utf-8") as fh:
            return "[tool.ruff" in fh.read()
    except OSError:
        return False


_PRETTIER_RC = (".prettierrc", ".prettierrc.json", ".prettierrc.yaml", ".prettierrc.yml",
                ".prettierrc.js", ".prettierrc.cjs", ".prettierrc.mjs", ".prettierrc.toml",
                "prettier.config.js", "prettier.config.cjs", "prettier.config.mjs")


def _prettier(path: str, top: str) -> "list | None":
    """The project's own prettier, when the project uses prettier."""
    if not _find(path, top, _PRETTIER_RC):
        d = _find(path, top, ("package.json",))
        pkg = {}
        if d:
            try:
                with open(os.path.join(d, "package.json"), encoding="utf-8") as fh:
                    pkg = json.load(fh)
            except (OSError, ValueError):
                pkg = {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        if "prettier" not in pkg and "prettier" not in deps:
            return None
    d = _find(path, top, (os.path.join("node_modules", ".bin", "prettier"),))
    if d:
        return [os.path.join(d, "node_modules", ".bin", "prettier")]
    exe = shutil.which("prettier")
    return [exe] if exe else None


_PRETTIER_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".scss",
                 ".json", ".md", ".yaml", ".yml", ".html", ".vue"}


def _formatter(path: str, top: str) -> "tuple[str, list] | None":
    """(name, command) of the formatter this file gets, or None."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".py", ".pyi") and _uses_ruff(path, top):
        r = _ruff()
        return ("ruff", [*r, "format", "--quiet", "--force-exclude"]) if r else None
    if ext in _PRETTIER_EXT:
        p = _prettier(path, top)
        return ("prettier", [*p, "--write"]) if p else None
    if ext == ".go" and shutil.which("gofmt") \
            and "vendor" not in os.path.relpath(os.path.realpath(path),
                                                os.path.realpath(top)).split(os.sep):
        return ("gofmt", ["gofmt", "-w"])
    # No rustfmt: it follows `mod x;` and rewrites the whole crate.
    return None


def _inside(path: str, top: str) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(top) + os.sep)


def format_file(path: str, cwd: str) -> "str | None":
    """Format ``path`` with the project's formatter. Returns the formatter's
    name when it CHANGED the file (the model's copy is stale then), else None."""
    if not _on("AIFORGE_CHAT_POST_EDIT_FORMAT") or not os.path.isfile(path) \
            or not _inside(path, cwd):
        return None
    fmt = _formatter(path, cwd)
    if not fmt:
        return None
    label, cmd = fmt
    try:
        with open(path, "rb") as fh:
            before = fh.read()
    except OSError:
        return None
    # In place, like the format tool: hold the per-path lock the file tools
    # hold, so a threaded read never caches a half-written file.
    from aiforge_core.runtime.doer_tools._fs import _file_lock
    with _file_lock(path):
        p = _run([*cmd, path], cwd)
    if p is None or p.returncode != 0:
        return None
    try:
        with open(path, "rb") as fh:
            changed = fh.read() != before
    except OSError:
        return None
    return label if changed else None


_RUFF_LINE = re.compile(r"^.+:\d+:\d+: [A-Z]+\d+ ")


#: Bases that add no methods a class would call on itself.
_PLAIN_BASES = frozenset({"object", "Generic", "Protocol", "ABC", "Exception",
                          "BaseException"})


def _missing_self_methods(path: str) -> list:
    """``self.name(...)`` calls to a method the class never defines.

    tomlkit (fresh-repo bench): an edit called ``self._can_extend_table(...)``
    in ``Parser``, which has no such method; ruff cannot see attributes, 49
    tests failed with AttributeError and the last 40 calls of the hour went to
    grepping for ``def _can_extend_table``. Only classes whose bases are all in
    this file (or plain) are checked, and not ones with ``__getattr__`` or a
    metaclass: there an inherited or dynamic method is possible.
    """
    import ast
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError, RecursionError):
        return []
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}

    def own(cls) -> set:
        names = set()
        for node in ast.walk(cls):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                  and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")):
                names.add(node.attr)
        return names

    def known(cls, seen=()) -> "set | None":
        if cls.name in seen or any(k.arg == "metaclass" for k in cls.keywords):
            return None
        names = own(cls)
        if names & {"__getattr__", "__getattribute__"}:
            return None
        for b in cls.bases:
            bname = b.id if isinstance(b, ast.Name) else (
                b.value.id if isinstance(b, ast.Subscript) and isinstance(b.value, ast.Name)
                else None)
            if bname in _PLAIN_BASES:
                continue
            if bname not in classes:
                return None             # inherits from elsewhere: anything may exist
            more = known(classes[bname], (*seen, cls.name))
            if more is None:
                return None
            names |= more
        return names

    def base_names(cls) -> set:
        return {b.id if isinstance(b, ast.Name) else getattr(getattr(b, "value", None), "id", "")
                for b in cls.bases}

    def below(name, seen=()) -> set:
        """Names the classes deriving from ``name`` in this file define: a
        base may call a method only its subclasses have (click
        ``Parameter._parse_decls``)."""
        out = set()
        for c in classes.values():
            if name in base_names(c) and c.name not in seen:
                out |= own(c) | below(c.name, (*seen, c.name))
        return out

    def mixed_in(name) -> bool:
        """A mixin: named so, or combined with other bases in this file
        (``class T(PeekableMixinTests, TestCase)``) — its methods come from the
        class it is mixed into."""
        return "Mixin" in name or any(
            name in base_names(c) and len(c.bases) > 1 for c in classes.values())

    out = []
    for cls in classes.values():
        if mixed_in(cls.name):
            continue
        names = known(cls)
        if names is None:
            continue
        names |= below(cls.name)
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.args.args:
                continue
            if any(getattr(d, "id", None) == "staticmethod" for d in fn.decorator_list):
                continue
            me = fn.args.args[0].arg
            if me not in ("self", "cls"):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == me
                        and not node.func.attr.startswith("__")
                        and node.func.attr not in names):
                    out.append(f"{path}:{node.lineno}:{node.col_offset + 1}: AF001 "
                               f"`{cls.name}` has no method `{node.func.attr}` "
                               f"(called as `{me}.{node.func.attr}(...)`): define it or "
                               "call a method that exists")
    return out


def _self_attrs(text: str) -> "tuple[set, dict] | None":
    """(names the file sets or defines, {attr: [lines that read self.attr]})."""
    import ast
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    defined, reads = set(), {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self":
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                defined.add(node.attr)
            else:
                reads.setdefault(node.attr, []).append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.isidentifier():
            defined.add(node.value)        # setattr(self, "name", ...), __slots__
    return defined, reads


def dropped_assignments(path: str, before: str, now: str) -> list:
    """``self.x`` set in ``before``, set nowhere in ``now``, still read there.

    marshmallow fx2a: adding two parameters to ``Field.__init__`` the model
    dropped ``self.validators = ...``; ``self.validators.insert(...)`` then
    failed for every field and the package no longer imported (0/11).
    """
    old, new = _self_attrs(before), _self_attrs(now)
    if not old or not new:
        return []
    out = []
    for attr in sorted((old[0] - new[0]) & set(new[1])):
        lines = new[1][attr]
        if attr.startswith("__"):
            continue
        out.append(f"{path}:{lines[0]}:1: AF002 `self.{attr} = ...` was removed, but "
                   f"`self.{attr}` is still read (line(s) {', '.join(map(str, lines[:5]))}): "
                   "put the assignment back or change those reads")
    return out


def _py_errors(path: str, cwd: str) -> "list | None":
    missing = _missing_self_methods(path)
    r = _ruff()
    if not r:
        return missing or None
    # Errors only: syntax, undefined/unused-before names, bad comparisons —
    # the classic flake8 "will crash" set, never style.
    # The project's config stays in (its builtins, per-file ignores);
    # --select narrows the rules to errors.
    p = _run([*r, "check", "--no-cache", "--force-exclude", "--output-format", "concise",
              "--select", "E9,F63,F7,F82", path], cwd)
    if p is None or p.returncode not in (0, 1):
        return missing or None   # a broken ruff config: not checked, not "clean"
    # "path:line:col: F821 Undefined name `x`" — the summary lines go.
    return [ln for ln in p.stdout.splitlines() if _RUFF_LINE.match(ln)] + missing


_TSC_LINE = re.compile(r"^[^\s(][^(]*\(\d+,\d+\): error TS")


def _ts_errors(path: str, cwd: str) -> "list | None":
    root = _find(path, cwd, ("tsconfig.json",))
    # tsc may be hoisted to a parent node_modules (a monorepo).
    bindir = _find(path, cwd, (os.path.join("node_modules", ".bin", "tsc"),))
    if not root or not bindir or root in _SLOW_TSC:
        return None
    tsc = os.path.join(bindir, "node_modules", ".bin", "tsc")
    t0 = time.monotonic()
    p = _run([tsc, "--noEmit", "--pretty", "false", "-p", root], root, timeout=60)
    if p is None or time.monotonic() - t0 > _TSC_SLOW_S:
        _SLOW_TSC.add(root)
    if p is None:
        return None
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root)).replace(os.sep, "/")
    lines = p.stdout.splitlines()
    if p.returncode != 0 and not any(_TSC_LINE.match(ln) for ln in lines):
        return None          # a tsconfig error (no file named): not checked, not "clean"
    return [ln for ln in lines if ln.startswith(rel + "(") and "error TS" in ln]


def _go_errors(path: str, cwd: str) -> "list | None":
    if not shutil.which("go") or not _find(path, cwd, ("go.mod",)):
        return None
    p = _run(["go", "vet", "."], os.path.dirname(path))
    if p is None:
        return None
    if p.returncode == 0:
        return []
    # "./main.go:4:7: …" or "vet: ./main.go:4:7: …" — this file, not other_main.go.
    mine = re.compile(r"(?:^|[\s/])" + re.escape(os.path.basename(path)) + r":\d+:\d+:")
    return [ln for ln in p.stderr.splitlines() if mine.search(ln)]


_CHECKERS = {".py": _py_errors, ".pyi": _py_errors, ".ts": _ts_errors, ".tsx": _ts_errors,
             ".go": _go_errors}


def diagnostics(path: str, cwd: str) -> "list | None":
    """Error lines for ``path`` only: empty when clean, None when nothing was
    checked (off, unsupported, the checker missing, failed or timed out)."""
    if not _on("AIFORGE_CHAT_POST_EDIT_DIAGNOSTICS") or not os.path.isfile(path):
        return None
    check = _CHECKERS.get(os.path.splitext(path)[1].lower())
    if not check:
        return None
    try:
        out = check(path, cwd)
    except Exception as exc:  # noqa: BLE001 — a checker never breaks a step
        log.debug("post-edit diagnostics failed (%s): %s", path, exc)
        return None
    return None if out is None else out[:MAX_LINES]


__all__ = ["MAX_LINES", "diagnostics", "format_file"]
