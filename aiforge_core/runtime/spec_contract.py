"""The names a request spells out, checked against the code before a turn ends.

A long request often states its API exactly: "`Inventory.receive(sku,
quantity, ref=None, note="", *, batch_id=None, expiry=None)`", "new module
`shopkit/invoices.py`", "`MovementKind` gains `RESTOCK`". A run that ended
on "all parts completed" had `receive` without `expiry`, no `return_items`,
no invoices module — and its own tests passed against its own API. The
model's word ("done") and an LLM read of the reply both missed it.

This reads the backticked names out of the request and looks for each in the
Python code under the work folder (``ast``, no import, no run):

- ``Cls.meth(a, b=1, *, c=None)`` — class ``Cls`` has ``meth``; when the
  span is written as a signature (a default, an annotation, a ``*``), it
  takes every named parameter;
- ``func(...)`` — a function ``func`` defined or imported somewhere;
- ``Cls.NAME`` — ``Cls`` has ``NAME`` (method, attribute, ``self.NAME``,
  enum member), here or in a base class the index has;
- ``pkg.mod.Name`` — ``pkg/mod.py`` defines ``Name`` (only for a package
  that lives in the work folder);
- ``path/to/file.py`` — the file exists.

It says only what it can know. Names after remove / delete / rename /
replace / deprecate are what the user wants GONE and are not looked for.
A class with a base outside the index (a framework's), a module with
``__getattr__`` or ``import *``, a name imported from a library, a tree that
is mostly not Python, an index cut short by its size: all unverifiable, so
nothing is said about them. Off switch: ``AIFORGE_CHAT_SPEC_CONTRACT=0``.
"""
from __future__ import annotations

import ast
import builtins
import logging
import os
import re
import sys

log = logging.getLogger("aiforge.spec_contract")

#: Python files read at most; past that only file paths are checked.
MAX_FILES = 3000
#: Files of any kind looked at while walking (a huge data folder ends the walk).
MAX_WALK = 30000
_MAX_BYTES = 400_000
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox",
              "build", "dist", ".mypy_cache", ".pytest_cache", ".aiforge", "site-packages"}
_SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".rs",
               ".cs", ".rb", ".php", ".swift", ".scala", ".c", ".cc", ".cpp", ".h"}
#: Below this share of Python among the source files, the tree is not Python.
_MIN_PY_SHARE = 0.3

_SPAN = re.compile(r"`([^`\n]{2,400})`")
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_CALL = re.compile(rf"^((?:{_IDENT}\.)*{_IDENT})\((.*)\)(?:\s*->.*)?$", re.S)
_DOTTED = re.compile(rf"^{_IDENT}(?:\.{_IDENT})+$")
_PYFILE = re.compile(r"^[\w./-]+\.py$")
# What the user wants gone: the names right after these words are not claims.
_GONE = re.compile(r"\b(remove[sd]?|delete[sd]?|drop(?:s|ped)?|rename[sd]?|replace[sd]?|"
                   r"deprecat\w*|instead of|get rid of|no longer|retire[sd]?|"
                   r"move[sd]?|split|merge[sd]?)\b", re.I)
_GONE_WINDOW = 60
# Bases that bring no member a request would name: an `Enum`'s members are
# its own, so `MovementKind.RESTOCK` is still checked.
_PLAIN_BASES = {"object", "Generic", "Protocol", "ABC", "Enum", "IntEnum", "StrEnum",
                "Flag", "IntFlag", "NamedTuple", "TypedDict", "str", "int", "float",
                "dict", "list", "tuple", "set", "Exception", "BaseException",
                "ValueError", "KeyError", "TypeError", "RuntimeError", "LookupError"}
#: A function the code lacks is said only for a signature, or in a tree this
#: much Python (a call in a mixed repo may be JS: `useEffect(...)`).
_FUNC_PY_SHARE = 0.8
_STDLIB = set(getattr(sys, "stdlib_module_names", ()))


#: A sentence about the code as it is now ("its current support lives in
#: `a.py` and is wired through `b()`"): its names are not claims.
_CURRENT = re.compile(r"\bcurrent(?:ly)?\b[^.`\n]{0,60}\b(?:lives?|is|are|uses?|wired|"
                      r"implemented|exists?|sits?|handled)\b", re.I)
#: After a current-state phrase, these say the sentence asks for something.
_ASKS = re.compile(r"\b(?:add|implement|create|must|should|make|use|return|raise|"
                   r"support|define|write|expose|accept|take)\b", re.I)
#: Gone rules, read from the text BETWEEN backticked spans (a span's own
#: text never counts: `Inventory.remove(sku)` is a name, not a verb). They
#: are conservative on purpose: a name is gone only when it is plainly the
#: OBJECT of a removal ("remove the old `X`"); anything unclear stays a
#: claim, and the model can answer "not required: X" to a wrong one
#: (_contract_gate) — one short reply, where a missed requirement is lost.
_REMOVE_VERB = re.compile(r"\b(?:remove[sd]?|delete[sd]?|drop(?:s|ped)?|deprecate[sd]?|"
                          r"get rid of|retire[sd]?|instead of)\b(?!-)", re.I)
#: Transform verbs: the SOURCE goes, the target after to/into/with stays.
_TRANSFORM_VERB = re.compile(r"\b(?:rename[sd]?|move[sd]?|replace[sd]?|merge[sd]?)\b(?!-)",
                             re.I)
_TARGET_PREP = re.compile(r"\b(?:to|into|with|onto|by)\b", re.I)
#: Between a removal verb and its object only these may stand.
_OBJ_FILLER = re.compile(
    r"^(?:\s|[,:(]|\b(?:the|a|an|old|older|legacy|deprecated|unused|obsolete|existing|"
    r"current|previous|v\d+|function|functions|method|methods|class|classes|module|"
    r"modules|file|files|helper|helpers|alias|aliases|attribute|attributes|field|fields|"
    r"calls?\s+to|uses?\s+of|support\s+for|any|all|both)\b)*$", re.I)
#: "remove the v1 client methods, i.e. `a`", "remove the old helpers: `a`":
#: a short noun phrase, then the list it names.
_NAMED_LIST = re.compile(r"[\w\s-]{0,50}(?:,?\s*i\.e\x00|:)\s*", re.I)
_NEGATED = re.compile(r"\b(?:not|never|don'?t|do not|keep|without)\b", re.I)
#: A gap that only joins list items on one line: the next name shares the verb.
_JOINER = re.compile(r"^[ \t,()]*(?:(?:and|or|as well as|along with|together with|plus)\b)?"
                     r"[ \t,()]*$", re.I)
#: Right after a name, passive only: "`x` is removed", "`a.py` (remove or
#: replace)", "`f()` was renamed to", "`X`. Remove it." An active verb describes
#: behaviour ("`prune()` deletes old rows", "`new.py` replaces `old.py`"), and
#: "renamed from" points at the OTHER name.
_GONE_AFTER = re.compile(
    r"^[\s`'\",)]*(?:"
    r"\(\s*(?:remove|delete|drop|deprecate)"
    r"|[:—–]\s*(?:remove|delete|drop)\b"
    r"|[.;]\s*(?:remove|delete|drop)\s+(?:it|this|them)\b"
    r"|(?:(?:is|are|was|were|has been|have been|gets?|must be|needs? to be|is to be|"
    r"should be|will be|can be)\s+)?(?:removed|deleted|dropped|deprecated|retired|renamed|"
    r"moved|replaced)\b(?!\s+(?:from|by)\b)"
    r"|(?:is|are)\s+no longer\b|goes away\b)", re.I)
_ABBREV = re.compile(r"\b(?:i\.e|e\.g|etc|vs|cf)\.\s", re.I)


def _clause(gap: str) -> str:
    """The gap's last clause: after a newline or sentence end (an
    abbreviation like "i.e. " or "e.g. " does not end one)."""
    return re.split(r"\n|[.;]\s", _ABBREV.sub(lambda m: m.group(0).replace(". ", "\x00 "), gap))[-1]


def _gone_names(request: str) -> set:
    """Backticked names the request says go (never claims)."""
    spans = list(_SPAN.finditer(request))
    n = len(spans)
    gone = [False] * n
    mode = [None] * n            # "rm" / "src" (rename source) / "dst" (rename target)
    prev_end = 0
    for i, m in enumerate(spans):
        gap = request[prev_end:m.start()]
        clause = _clause(gap)
        nxt_gap = request[m.end():spans[i + 1].start()] if i + 1 < n else ""
        rm = list(_REMOVE_VERB.finditer(clause))
        tf = list(_TRANSFORM_VERB.finditer(clause))
        verb = max(rm + tf, key=lambda v: v.start(), default=None)
        if i and mode[i - 1] == "src" and re.fullmatch(r"\s*(?:to|into|with|onto|by)\s*",
                                                     gap, re.I):
            mode[i] = "dst"                            # "rename `a` to `b`": b is new
        elif i and _JOINER.match(gap) and "\n" not in gap and mode[i - 1]:
            # a list shares the verb: "remove `a` along with `b`";
            # "rename `a` to `b` and `c` to `d`" — `c` is a source again
            if mode[i - 1] == "rm":
                mode[i], gone[i] = "rm", True
            elif mode[i - 1] == "dst" and re.fullmatch(
                    r"\s*(?:to|into|with|onto|by)\s*", nxt_gap, re.I):
                mode[i], gone[i] = "src", True
            elif mode[i - 1] == "src":
                mode[i], gone[i] = "src", True
        elif verb is not None and not _NEGATED.search(clause[:verb.start()]) \
                and not re.search(r"\b(?:a|an)\s*$", clause[:verb.start()]):
            between = clause[verb.end():]
            if verb in rm and (_OBJ_FILLER.match(between) or _NAMED_LIST.fullmatch(between)):
                mode[i], gone[i] = "rm", True          # "remove the old `X`"
            elif verb in tf:
                if _TARGET_PREP.search(between):
                    mode[i] = "dst"                    # "… to `Y`": the new name
                elif _OBJ_FILLER.match(between):
                    mode[i], gone[i] = "src", True     # "rename `X` …"
        prev_end = m.end()
    chain = False
    for i in range(n - 1, -1, -1):
        m = spans[i]
        nxt = spans[i + 1].start() if i + 1 < n else len(request)
        tail = request[m.end():min(nxt, m.end() + 80)]
        if _GONE_AFTER.match(tail):
            gone[i] = chain = True
        elif chain and _JOINER.match(tail) and "\n" not in tail:
            gone[i] = True                             # "`a()`, `b()` and `c()` are removed"
        else:
            chain = False
    return {m.group(1).strip() for m, g in zip(spans, gone) if g}


#: "Examples (with `_get_words(cli, args)` …", "e.g. `helper(x)`": a name used
#: to show behaviour, not one the code must define.
_EXAMPLE = re.compile(r"\b(?:examples?|e\.g\.|for instance)\b", re.I)


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_SPEC_CONTRACT", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _params(text: str) -> list:
    """Parameter names of a written signature: ``a, b: int = 1, *, c=None``."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    names = []
    for part in out:
        m = re.match(rf"\s*\**({_IDENT})\s*(?:[:=]|$)", part)
        if m and m.group(1) not in ("self", "cls"):
            names.append(m.group(1))
    return names


def _is_signature(params_text: str) -> bool:
    """``f(a, b=1)`` / ``f(a: int)`` / ``f(a, *, b)`` is a signature; ``f(data)``
    may be a call site quoted from the code ("`process(data)` crashes")."""
    return bool(re.search(r"[=:*]", params_text))


def _arrow_pair(request: str, m, s: str) -> bool:
    """`a()` -> `A()`: an input/output pair of examples — both sides spans and
    this side no signature (`parse(text: str)` -> `Tree` is a claim)."""
    c = _CALL.match(s)
    if c and _is_signature(c.group(2)) and _params(c.group(2)) and all(
            re.fullmatch(r"\*{0,2}[A-Za-z_]\w*", p) for p in _params(c.group(2))):
        return False          # `parse(text: str)` -> `Tree`: a signature
    after = request[m.end():m.end() + 6]
    before = request[max(0, m.start() - 6):m.start()]
    return bool(re.match(r"\s*(?:->|→|=>)\s*`", after) or re.search(r"`\s*(?:->|→|=>)\s*$", before))


def claims(request: str) -> list:
    """What the request says the code has, one dict per backticked name."""
    out, seen = [], set()
    request = request or ""
    gone = _gone_names(request)
    for m in _SPAN.finditer(request):
        s = m.group(1).strip()
        if s in seen or s in gone:
            continue          # "remove `X.y`", "`a.py` is no longer needed": not a claim
        seen.add(s)
        before = (request[max(0, m.start() - _GONE_WINDOW):m.start()]).split("\n")[-1]
        if _EXAMPLE.search(before) or _arrow_pair(request, m, s):
            seen.discard(s)   # "Examples (with `_get_words(cli)` …", "`a()` -> `A()`":
            continue          # shown behaviour — a later mention may still claim it
        sentence = re.split(r"[.!?]\s|\n\s*\n|\n\s*(?:[-*#]|\d+\.)\s",
                            request[max(0, m.start() - 200):m.start()])[-1]
        cur = _CURRENT.search(sentence)
        if cur and not _ASKS.search(sentence[cur.end():]):
            seen.discard(s)   # "its current support lives in `x.py`": what IS, not
            continue          # a claim — a later mention may still claim it
        if _PYFILE.match(s) and "/" in s:
            out.append({"kind": "file", "path": s, "text": s})
            continue
        c = _CALL.match(s)
        if c:
            name, ptext = c.group(1), c.group(2)
            params = _params(ptext) if _is_signature(ptext) else []
            parts = name.split(".")
            if len(parts) == 1:
                out.append({"kind": "func", "name": name, "params": params, "text": s})
            elif parts[-2][:1].isupper():
                out.append({"kind": "method", "cls": parts[-2], "name": parts[-1],
                            "params": params, "text": s})
            elif len(parts) >= 3 and all(p[:1].islower() for p in parts[:-1]):
                # `click.shell_completion.ShellComplete(cli, prog_name)`: the
                # module must export it (a class split into _shell_base.py
                # that shell_completion never imports fails every import).
                # A lowercase name may be a call on a library object
                # (`app.config.from_object(c)`): checked only when the module
                # resolves to a file of the tree.
                out.append({"kind": "export", "module": parts[:-1], "name": parts[-1],
                            "text": name, "soft": not parts[-1][:1].isupper()})
            continue
        if not _DOTTED.match(s):
            continue
        parts = s.split(".")
        if parts[0][:1].isupper() and len(parts) == 2:
            out.append({"kind": "member", "cls": parts[0], "name": parts[1], "text": s})
        elif parts[-1][:1].isupper() and all(p[:1].islower() for p in parts[:-1]) \
                and len(parts) >= 3:
            out.append({"kind": "export", "module": parts[:-1], "name": parts[-1], "text": s})
    return out


class _Index:
    """Classes (members, bases), functions (parameters), imported names and
    what each module defines, from the Python files under ``root``."""

    def __init__(self, root: str):
        self.root = root
        self.classes: dict = {}      # name -> {member: params | None}
        self.bases: dict = {}        # name -> [base names]
        self.funcs: dict = {}        # name -> [params, ...]
        self.stubs: set = set()      # (class or "", name) whose body is only a stub
        self.imported: set = set()   # names brought in by `from x import y`
        self.modules: dict = {}      # rel path -> set of top-level names
        self.open_modules: set = set()   # rel paths with __getattr__ / import *
        self.truncated = False
        self.py = self.source = 0
        walked = 0
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and not x.startswith(".")]
            for f in files:
                walked += 1
                if walked > MAX_WALK:
                    self.truncated = True
                    return
                ext = os.path.splitext(f)[1].lower()
                if ext in _SOURCE_EXT:
                    self.source += 1
                if ext != ".py":
                    continue
                self.py += 1
                if self.py > MAX_FILES:
                    self.truncated = True
                    return
                self._add(os.path.join(d, f))

    def _add(self, path: str) -> None:
        try:
            if os.path.getsize(path) > _MAX_BYTES:
                return
            with open(path, encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError, ValueError):
            return
        rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        top = self.modules.setdefault(rel, set())
        for node in _top_level(tree.body):
            for name in _defined(node):
                top.add(name)
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                self.open_modules.add(rel)
            if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
                self.open_modules.add(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.imported.update(a.asname or a.name for a in node.names)
            elif isinstance(node, ast.ClassDef):
                self._add_class(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs.setdefault(node.name, []).append(_arg_names(node))
        for node in _top_level(tree.body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _stub(node) \
                    and not _meant_empty(node):
                self.stubs.add(("", node.name))

    def _add_class(self, node: ast.ClassDef) -> None:
        members = self.classes.setdefault(node.name, {})
        for b in node.bases:
            base = b.id if isinstance(b, ast.Name) else (
                b.attr if isinstance(b, ast.Attribute) else "?")
            # `class Session(requests.Session)`: the base is not this class.
            self.bases.setdefault(node.name, []).append("?" if base == node.name else base)
        abstract_cls = any(_base_name(b) in _ABSTRACT_BASES for b in node.bases) or any(
            k.arg == "metaclass" for k in node.keywords)
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                members[item.name] = _arg_names(item)
                if _stub(item) and not abstract_cls and not _meant_empty(item):
                    self.stubs.add((node.name, item.name))
                else:
                    self.stubs.discard((node.name, item.name))
                # attributes the methods set: self.items = {}
                for sub in ast.walk(item):
                    if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Store) \
                            and isinstance(sub.value, ast.Name) and sub.value.id == "self":
                        members.setdefault(sub.attr, None)
            else:
                for name in _defined(item):
                    members.setdefault(name, None)

    def member(self, cls: str, name: str, _seen=None):
        """``(True, params)`` when ``cls`` (or a base the index has) has
        ``name`` (params None: an attribute); ``(True, "MISSING")`` when it
        surely does not; ``(False, None)`` when a base outside the index
        might supply it."""
        _seen = _seen if _seen is not None else set()
        if cls in _seen:
            return True, "MISSING"      # a diamond: that path is told already
        _seen.add(cls)
        members = self.classes.get(cls, {})
        if name in members:
            return True, members[name]
        unknown = False
        for base in self.bases.get(cls, []):
            if base in _PLAIN_BASES:
                continue
            if base not in self.classes:
                unknown = True
                continue
            known, params = self.member(base, name, _seen)
            if known and params != "MISSING":
                return True, params
            unknown = unknown or not known
        return (False, None) if unknown else (True, "MISSING")


def _stub(fn) -> bool:
    """A def whose body, past a docstring, is only ``raise NotImplementedError``
    or ``...``: the name is there, the behaviour is not. A click run ended at
    0/37 with ``ShellComplete.get_completions`` a stub while the check called
    every named part present. (``pass`` is not: a no-op hook is often meant.)"""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 1:
        return False
    b = body[0]
    if isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and b.value.value is Ellipsis:
        return True
    if isinstance(b, ast.Raise) and b.exc is not None:
        exc = b.exc.func if isinstance(b.exc, ast.Call) else b.exc
        return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


#: A class built to have bodiless methods (an interface, an ABC).
_ABSTRACT_BASES = {"Protocol", "ABC", "ABCMeta"}
_EMPTY_DECORATORS = {"abstractmethod", "abstractproperty", "overload"}


def _base_name(b) -> str:
    """``Protocol`` for ``Protocol``, ``typing.Protocol`` and ``Protocol[T]``."""
    if isinstance(b, ast.Subscript):
        b = b.value
    return b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")


def _meant_empty(fn) -> bool:
    """``@abstractmethod`` / ``@overload``: a body of ``...`` is the point."""
    for d in fn.decorator_list:
        d = d.func if isinstance(d, ast.Call) else d
        name = d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
        if name in _EMPTY_DECORATORS:
            return True
    return False


#: Near a name, the request says it is meant to stay unimplemented here.
#: The design the request states for a bodiless name ("(abstract in the base
#: class)", "subclasses implement it", "a no-op"). Words that describe the
#: stub as it is now ("currently raises NotImplementedError", "the placeholder
#: in f()") are not it: that line asks for the body.
_ABSTRACT = re.compile(r"\(abstract\b|\b(?:is|are|stays?|remains?)\s+abstract\b|"
                       r"\babstract\s+(?:in|on)\s+the\s+base\b|"
                       r"\bsubclass(?:es)?\s+(?:must\s+|should\s+)?(?:implement|override)|"
                       r"NotImplementedError\b[^.\n]{0,40}\bbase\b|"
                       r"\bno-?op\b|\bdoes nothing\b", re.I)
_NOW = re.compile(r"\b(?:currently|still|right now|today|at the moment)\b", re.I)


def _meant_abstract(request: str, text: str) -> bool:
    """The request says this name is abstract / raises NotImplementedError /
    does nothing, on a line that names it ("- `format_completion(item)` ->
    str (abstract in the base class).")."""
    req = request or ""
    for m in re.finditer(re.escape(text), req):
        line = req[req.rfind("\n", 0, m.start()) + 1:]
        nl = line.find("\n")
        line = line if nl < 0 else line[:nl]
        if _ABSTRACT.search(line) and not _NOW.search(line):
            return True
    return False


def _guard_only(test) -> bool:
    """``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` / ``if __name__ ==
    "__main__":`` — a body that is no runtime export."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    if not (isinstance(test, ast.Compare) and len(test.comparators) == 1
            and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = [test.left, test.comparators[0]]
    return (any(isinstance(x, ast.Name) and x.id == "__name__" for x in sides)
            and any(isinstance(x, ast.Constant) and x.value == "__main__" for x in sides))


def _top_level(body) -> list:
    """Module-level statements, the bodies of top-level ``if``/``try``/``with``
    blocks included (``try: from ._speedups import f`` / ``except: def f``)."""
    out = []
    for node in body:
        out.append(node)
        if isinstance(node, ast.If) and _guard_only(node.test):
            out += _top_level(node.orelse)   # typing-only / script body: not exports
            continue
        if isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp) \
                and isinstance(node.test.op, ast.Not) and _guard_only(node.test.operand):
            out += _top_level(node.body)     # `if not TYPE_CHECKING:` runs; else is typing
            continue
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            for part in ("body", "orelse", "finalbody"):
                out += _top_level(getattr(node, part, None) or [])
            for h in getattr(node, "handlers", None) or []:
                out += _top_level(h.body)
    return out


def _defined(node) -> list:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in node.names]
    return []


def _arg_names(fn) -> list:
    a = fn.args
    names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    if a.vararg:
        names.append(a.vararg.arg)
    if a.kwarg:
        names.append("**")
    return names


def _takes(have, want: list) -> list:
    """Parameters of ``want`` the signature ``have`` lacks (``**kw`` takes all)."""
    if not isinstance(have, list) or "**" in have:
        return []
    return [p for p in want if p not in have]


def _local_package(module: list, ix: "_Index") -> bool:
    top = module[0]
    if top in _STDLIB:
        return False
    return any(p == top + ".py" or p.startswith(top + "/") or ("/" + top + "/") in p
               for p in ix.modules)


def _declared(request: str, cs: list, ix: "_Index") -> set:
    """Classes the request says the code defines: a ``pkg.mod.Cls`` path of a
    package in the work folder, or "class/dataclass/enum/new … `Cls`". A
    method of any other class the code lacks is a library's, not a gap."""
    out = {c["name"] for c in cs if c["kind"] == "export" and _local_package(c["module"], ix)}
    for m in re.finditer(rf"\b(?:class|dataclass|enum|exception|new)\b[^`\n]{{0,40}}`({_IDENT})`",
                         request or "", re.I):
        out.add(m.group(1))
    return out


def _module_names(module: list, ix: "_Index"):
    base = "/".join(module)
    for p in (base + ".py", base + "/__init__.py"):
        if p in ix.modules:
            return p, ix.modules[p]
    for p, names in ix.modules.items():      # a src/ layout
        if p.endswith("/" + base + ".py") or p.endswith("/" + base + "/__init__.py"):
            return p, names
    return None, None


_NOT_SOURCE_DIRS = {"tests", "test", "fixtures", "examples", "docs", "doc", "testdata"}


def _all_files(ix) -> "list | None":
    """Every file path under the index root ("/"-joined, relative), walked once
    per index; None when the tree is too big to list."""
    got = getattr(ix, "_files", False)
    if got is not False:
        return got
    out, walked = [], 0
    for d, dirs, files in os.walk(ix.root):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and not x.startswith(".")]
        for f in files:
            walked += 1
            if walked > MAX_WALK:
                ix._files = None
                return None
            out.append(os.path.relpath(os.path.join(d, f), ix.root).replace(os.sep, "/"))
    ix._files = out
    return out


def _file_under(ix, path: str) -> bool:
    """``path`` exists under the root as written, or deeper as a package path
    (`commands/export.py` is src/mytool/commands/export.py) — never a
    same-named file in a tests/fixtures/docs folder the path does not name."""
    root = ix.root
    rel = path[2:] if path.startswith("./") else path
    if os.path.isfile(os.path.join(root, rel)):
        return True
    files = _all_files(ix)
    if files is None:             # too big to list: as written or one level down
        try:
            return any(os.path.isfile(os.path.join(root, d, rel)) for d in os.listdir(root))
        except OSError:
            return True
    own = set(rel.split("/")[:-1])
    for f in files:
        if f.endswith("/" + rel):
            extra = f[:-len(rel) - 1].split("/")
            if not (set(extra) & (_NOT_SOURCE_DIRS - own)):
                return True
    return False


def _no_file(ix, path: str) -> str:
    """The gap line for a missing file, naming the same file one or more
    folders up when it is there: a repo folder named like its package
    (``mini/`` holding ``mini/``) got ``stairs.py`` at the root, and the
    model, told only "mini/stairs.py does not exist", called the check stale
    twice. Only a strict tail of the requested folders counts
    (``mini/stairs.py`` → ``stairs.py``), never a same-named file in an
    unrelated folder (``blog/models.py`` is not ``accounts/models.py``)."""
    rel = path[2:] if path.startswith("./") else path
    want = rel.split("/")
    files = set(_all_files(ix) or [])
    if want[-1].startswith("__"):
        return f"{path} does not exist"      # __init__.py: one in every package
    for k in range(1, len(want)):
        near = "/".join(want[k:])
        if near in files:
            return (f"{path} does not exist ({HINT_LEAD}./{near}; if you wrote "
                    "it there by mistake, move it)")
    return f"{path} does not exist"


#: The lead of the misplaced-file hint in a gap line; ``_contract_gate``
#: strips the hint before it reads names from a gap.
HINT_LEAD = "the request's path is from the project root; there is "


def _check(c: dict, ix: _Index, declared: set) -> "str | None":
    k = c["kind"]
    if k == "file":
        return None if _file_under(ix, c["path"]) else _no_file(ix, c["path"])
    if k in ("method", "member"):
        if c["cls"] not in ix.classes:
            if c["cls"] in ix.imported or c["cls"] not in declared:
                return None
            return f"class {c['cls']} not found (for {c['text']})"
        known, params = ix.member(c["cls"], c["name"])
        if not known:
            return None                      # a framework base may supply it
        if params == "MISSING":
            return f"{c['cls']}.{c['name']} is missing"
        lacks = _takes(params, c.get("params") or [])
        if lacks:
            return f"{c['cls']}.{c['name']} does not take {', '.join(lacks)}"
        if (c["cls"], c["name"]) in ix.stubs and not _meant_abstract(
                c.get("request", ""), c["text"]):
            return (f"{c['cls']}.{c['name']} is only a stub (raise "
                    "NotImplementedError / ...): implement it")
        return None
    if k == "func":
        name = c["name"]
        if hasattr(builtins, name) or name in ix.imported:
            return None
        if name[:1].isupper() and name not in ix.classes:
            # `Decimal("1.5")`: a class called — a library's unless the
            # request says the code defines it.
            return f"class {name} not found (for {c['text']})" if name in declared else None
        sigs = ix.funcs.get(name)
        if not sigs:
            if name in ix.classes or not (c.get("params") or ix.py >= _FUNC_PY_SHARE * max(ix.source, 1)):
                return None
            return f"function {name} is missing"
        best = min((_takes(s, c.get("params") or []) for s in sigs), key=len)
        if best:
            return f"{name}() does not take {', '.join(best)}"
        if ("", name) in ix.stubs and len(sigs) == 1 and not _meant_abstract(
                c.get("request", ""), c["text"]):
            return f"{name}() is only a stub (raise NotImplementedError / ...): implement it"
        return None
    if k == "export":
        if not _local_package(c["module"], ix):
            return None                      # a library path: django.db.models.Model
        path, names = _module_names(c["module"], ix)
        if names is None and c.get("soft"):
            return None
        if names is None:
            return f"module {'.'.join(c['module'])} does not exist (for {c['text']})"
        if path in ix.open_modules:
            return None
        return None if c["name"] in names else f"{c['text']} is not defined"
    return None


#: An explicit rename of a helper: "rename `old` to `new`", "`new` …
#: (rename the existing `old` helper", "`new`, renamed from `old`". The
#: new name may be dotted (``click.core._fast_exit``): its module is scope.
_RENAME_TO = re.compile(r"\brename\s+(?:the\s+)?(?:existing\s+|old\s+|current\s+)?"
                        r"`([A-Za-z_]\w*)(?:\(\))?`\s+(?:(?:function|helper|method)\s+)?"
                        r"to\s+`([\w.]+?)(?:\([^`]*\))?`", re.I)
_RENAME_PAREN = re.compile(r"`([\w.]+?)(?:\([^`]*\))?`[^`.;\n]{0,60}?\(\s*rename\s+(?:the\s+)?"
                           r"(?:existing\s+|old\s+|current\s+)?`([A-Za-z_]\w*)(?:\(\))?`", re.I)
_RENAMED_FROM = re.compile(r"`([\w.]+?)(?:\([^`]*\))?`,?\s+renamed\s+from\s+`([A-Za-z_]\w*)(?:\(\))?`", re.I)
#: Wording in a rename's own clause that keeps the old name (or negates it).
_KEEPS_OLD = re.compile(r"\b(?:not|don'?t|never|keep|keeps|kept|alias|wrapper|shim|"
                        r"deprecated|instead\s+of|backward)\b", re.I)
#: The lead of the explanation a rename gap carries; stripped before names
#: are read from it (``_contract_gate._bare``).
RENAME_LEAD = "the request renames it"


def _clause_of(request: str, m) -> str:
    lo = max(request.rfind(c, 0, m.start()) for c in ".;\n")
    his = [i for i in (request.find(c, m.end()) for c in ".;\n") if i >= 0]
    return request[lo + 1:min(his) if his else len(request)]


def renames(request: str) -> list:
    """``[(old, new, module path or None)]`` the request renames, explicit
    wording only (never a list a remove verb swept up), bare function names;
    a clause that keeps the old name (alias, wrapper, deprecated, not) is no
    rename to check."""
    out = []
    req = request or ""
    for rx, o, n in ((_RENAME_TO, 1, 2), (_RENAME_PAREN, 2, 1), (_RENAMED_FROM, 2, 1)):
        for m in rx.finditer(req):
            clause = _clause_of(req, m).replace(m.group(0), " ")
            if _KEEPS_OLD.search(clause) or _KEEPS_OLD.search(m.group(0)):
                continue
            new = m.group(n)
            mod = (new.rsplit(".", 1)[0].replace(".", "/") + ".py") if "." in new else None
            out.append((m.group(o), new.rsplit(".", 1)[-1], mod))
    return [t for t in dict.fromkeys(out) if t[0] != t[1]]


def _calls(node, name: str) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
               for n in ast.walk(node))


_RENAME_CACHE: dict = {}


def _old_name_left(root: str, old: str, new: str, mod: "str | None" = None,
                   key=None) -> "str | None":
    """In the module that defines ``new`` at top level (the one the request
    names, when it does): where ``old`` is still a top-level def — the
    request's name, none of its behaviour. click t7 kept
    ``def fast_exit`` and its call and added ``_fast_exit = fast_exit``; the
    tests patch ``_fast_exit``, ``os._exit`` killed pytest, 0/37. A def of
    ``old`` that calls ``new`` is a shim, not a leftover. Methods, nested
    functions and other modules are never looked at."""
    ck = (root, key, old, new, mod)
    if key is not None and ck in _RENAME_CACHE:
        return _RENAME_CACHE[ck]
    found, n_files = None, 0
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and x not in _NOT_SOURCE_DIRS
                   and not x.startswith(".")]
        for f in files:
            if not f.endswith(".py") or f.startswith("test_") or f.endswith("_test.py") \
                    or f == "conftest.py":
                continue
            path = os.path.join(d, f)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if mod and not (rel == mod or rel.endswith("/" + mod)):
                continue
            n_files += 1
            if n_files > MAX_FILES:
                break
            try:
                if os.path.getsize(path) > _MAX_BYTES:
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    tree = ast.parse(fh.read())
            except (OSError, SyntaxError, ValueError, RecursionError):
                continue
            top = tree.body
            has_new = any(
                (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == new)
                or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == new
                                                      for t in n.targets))
                for n in top)
            if not has_new:
                continue
            for n in top:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == old \
                        and not _calls(n, new):
                    found = (f"{rel}:{n.lineno}", f"still defines {old}")
                    break
            if found:
                break
        if found or n_files > MAX_FILES:
            break
    out = None
    if found:
        out = (f"{old} -> {new} unfinished ({found[0]} {found[1]}; {RENAME_LEAD}: rename "
               "the def and its callers, not an alias)")
    if key is not None:
        if len(_RENAME_CACHE) > 64:
            _RENAME_CACHE.clear()
        _RENAME_CACHE[ck] = out
    return out


_CACHE: dict = {}


def _index(root: str, key) -> "_Index":
    """The index of ``root``, kept while ``key`` (the turn's edit count)
    stays the same: a finish tried again without an edit does not re-parse."""
    hit = _CACHE.get(root)
    if key is not None and hit and hit[0] == key:
        return hit[1]
    ix = _Index(root)
    _CACHE.clear()          # one tree at a time is enough
    _CACHE[root] = (key, ix)
    return ix


def missing(request: str, root: str, key=None) -> list:
    """Lines naming what the request spells out and the code lacks. Empty
    when everything is there, nothing can be told, or it is off. ``key``:
    changes when the files may have (an edit count) — same key, same index."""
    if not enabled() or not root or not os.path.isdir(root):
        return []
    cs = claims(request)
    if not cs:
        return []
    files = [c for c in cs if c["kind"] == "file"]
    code = [c for c in cs if c["kind"] != "file"]
    ix = None
    if code:
        try:
            ix = _index(root, key)
        except Exception as exc:  # noqa: BLE001 — the check never breaks a turn
            log.debug("spec contract index failed: %s", exc)
            ix = None
        if ix is None or ix.truncated or not ix.py \
                or ix.py < _MIN_PY_SHARE * max(ix.source, 1):
            code = []       # not (all of) a Python tree: only the files are told
    if ix is None:
        ix = _Index.__new__(_Index)
        ix.root = root
    declared = _declared(request, cs, ix) if code else set()
    out = []
    for c in files + code:
        c.setdefault("request", request)
        try:
            gap = _check(c, ix, declared)
        except Exception:  # noqa: BLE001
            gap = None
        if gap and gap not in out:
            out.append(gap)
    if code:
        for old_name, new_name, mod in renames(request):
            try:
                left = _old_name_left(root, old_name, new_name, mod, key)
            except Exception:  # noqa: BLE001
                left = None
            if left and left not in out:
                out.append(left)
    return out


__all__ = ["claims", "enabled", "missing", "renames"]
