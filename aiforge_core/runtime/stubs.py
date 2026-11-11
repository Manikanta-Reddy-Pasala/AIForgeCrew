"""Find functions left as stubs — a body that only says "not implemented".

An agent often scaffolds the central function first (``raise
NotImplementedError``, ``todo!()``, ``throw new Error("not implemented")``),
moves on to the edges, writes tests around what it has, and finishes with a
green suite and the core still empty. A compiler or linter is happy with a
stub, so nothing else in the edit loop says so.

Language-general: Python is read with ``ast``; every other language by the
"not implemented" marker each one uses, attributed to the nearest function
header above it. Abstract methods, interface/protocol declarations, ``.pyi``
stubs and ``@overload`` signatures are declarations, not stubs, and are left
alone. Only stubs a change ADDS count (``new_stubs``): a project's own
pre-existing ``NotImplementedError`` hooks are not the agent's to fill.
"""
from __future__ import annotations

import ast
import os
import re

# Per extension: the statement that marks an unimplemented body.
_NOT_IMPL_TEXT = r"""(?i:not\s*(?:yet\s*)?implemented|unimplemented|todo|fixme|stub)"""
_MARKERS = {
    "js": re.compile(r"\bthrow\s+(?:new\s+)?\w*Error\s*\(\s*[`'\"][^`'\"]*"
                     + _NOT_IMPL_TEXT),
    "java": re.compile(r"\bthrow\s+new\s+(?:java\.lang\.)?(?:UnsupportedOperationException"
                       r"\s*\(\s*(?:\)|\"[^\"]*" + _NOT_IMPL_TEXT + r")|"
                       r"(?:\w+\.)*NotImplemented(?:Exception|Error)\b)"),
    "kotlin": re.compile(r"(?:\bTODO\s*\(|\bthrow\s+(?:NotImplementedError|"
                         r"UnsupportedOperationException)\s*\()"),
    "csharp": re.compile(r"\bthrow\s+new\s+(?:System\.)?NotImplementedException\b"),
    "go": re.compile(r"\bpanic\s*\(\s*(?:\"[^\"]*|`[^`]*|errors\.New\(\"[^\"]*)"
                     + _NOT_IMPL_TEXT),
    "rust": re.compile(r"\b(?:todo|unimplemented)!\s*\("),
    "ruby": re.compile(r"\braise\s+NotImplementedError\b"),
    "php": re.compile(r"\bthrow\s+new\s+\\?(?:\w+\\)*(?:\w*Exception|\w*Error)\s*\(\s*['\"]"
                      r"[^'\"]*" + _NOT_IMPL_TEXT),
    "swift": re.compile(r"\bfatalError\s*\(\s*\"[^\"]*" + _NOT_IMPL_TEXT),
    "scala": re.compile(r"(?:=\s*\?\?\?|\bthrow\s+new\s+NotImplementedError\b)"),
    "cpp": re.compile(r"\bthrow\s+std::(?:logic_error|runtime_error)\s*\(\s*\"[^\"]*"
                      + _NOT_IMPL_TEXT),
}
_LANG = {".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js",
         ".tsx": "js", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
         ".cs": "csharp", ".go": "go", ".rs": "rust", ".rb": "ruby",
         ".php": "php", ".swift": "swift", ".scala": "scala", ".cc": "cpp",
         ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".h": "cpp"}
# A function header on one line, per language family, capturing the name.
_HEADERS = {
    "js": re.compile(r"(?:function\s*\*?\s*(\w+)|(\w+)\s*[:=]\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)"
                     r"|^\s*(?:(?:public|private|protected|static|async|get|set|override|readonly)\s+)*"
                     r"(\w+)\s*\([^)]*\)\s*(?::\s*[^={]+)?\{)"),
    "java": re.compile(r"^\s*(?:@\w+\s+)*(?:(?:public|private|protected|static|final|"
                       r"synchronized|abstract|default|override|open|suspend|internal|fun)\s+)*"
                       r"(?:[\w<>\[\],.?:*&\s]+\s+)?(\w+)\s*\("),
    "go": re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*[\[(]"),
    "rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:const\s+)?(?:unsafe\s+)?fn\s+(\w+)"),
    "ruby": re.compile(r"^\s*def\s+(?:self\.)?(\w+[?!=]?)"),
    "php": re.compile(r"function\s+&?\s*(\w+)\s*\("),
    "swift": re.compile(r"^\s*(?:@\w+\s+)*(?:(?:public|private|internal|open|static|"
                        r"override|mutating|final)\s+)*func\s+(\w+)"),
    "scala": re.compile(r"^\s*(?:override\s+)?def\s+(\w+)"),
}
for _k in ("kotlin", "csharp", "cpp"):
    _HEADERS[_k] = _HEADERS["java"]
_NOT_NAMES = frozenset({"if", "for", "while", "switch", "catch", "return", "new",
                        "throw", "else", "do", "try", "when", "match", "case",
                        "function", "super", "this", "sizeof"})
# Declarations that are SUPPOSED to have no body: abstract/interface members.
_ABSTRACT_LINE = re.compile(r"\babstract\b|\binterface\b|\btrait\b|\bprotocol\b|"
                            r"@abstractmethod|\bvirtual\b[^;{]*=\s*0")


def _is_abstract_py(fn: ast.AST, cls: "ast.ClassDef | None") -> bool:
    for d in getattr(fn, "decorator_list", []):
        name = d.attr if isinstance(d, ast.Attribute) else getattr(d, "id", "")
        if isinstance(d, ast.Call):
            f = d.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name in ("abstractmethod", "abstractproperty", "overload",
                    "abstractclassmethod", "abstractstaticmethod"):
            return True
    if cls is not None:
        for b in cls.bases:
            n = b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "")
            if isinstance(b, ast.Subscript):
                v = b.value
                n = v.attr if isinstance(v, ast.Attribute) else getattr(v, "id", "")
            if n in ("Protocol", "ABC"):
                return True
    return False


def _py_stub_kind(fn) -> str:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant) and isinstance(
            body[0].value.value, str):
        body = body[1:]                       # the docstring
    if len(body) != 1:
        return ""
    st = body[0]
    if isinstance(st, ast.Raise) and st.exc is not None:
        e = st.exc.func if isinstance(st.exc, ast.Call) else st.exc
        name = e.attr if isinstance(e, ast.Attribute) else getattr(e, "id", "")
        if name in ("NotImplementedError", "NotImplemented"):
            return "raises NotImplementedError"
    if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) \
            and st.value.value is Ellipsis:
        return "has only `...` as its body"
    return ""


def _py_stubs(text: str) -> "list | None":
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    out = []
    # A base-class hook its subclasses override (`raise NotImplementedError`
    # in Base.run, a real run in Child) is a design, not a stub.
    overridden: set = set()
    classes = [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef)]
    for c in classes:
        methods = {f.name for f in c.body
                   if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for b in c.bases:
            bn = b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "")
            overridden.update((bn, m) for m in methods)

    def visit(node, cls, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not _is_abstract_py(child, cls) and not (
                        cls is not None and (cls.name, child.name) in overridden):
                    kind = _py_stub_kind(child)
                    if kind:
                        out.append((f"{prefix}{child.name}", child.lineno, kind))
                visit(child, None, f"{prefix}{child.name}.")
    visit(tree, None, "")
    return out


_TYPE_HEAD = re.compile(r"\b(?:class|interface|trait|protocol|struct|enum|"
                        r"object|impl|module)\b")


def _in_abstract_type(lines: list, at: int) -> bool:
    """Whether the function at ``at`` sits in an interface / trait / protocol /
    abstract class: the nearest less-indented type header above it says so."""
    ind = len(lines[at]) - len(lines[at].lstrip())
    for j in range(at - 1, max(-1, at - 400), -1):
        ln = lines[j]
        if not ln.strip():
            continue
        if len(ln) - len(ln.lstrip()) < ind and _TYPE_HEAD.search(ln):
            return bool(re.search(r"\binterface\b|\btrait\b|\bprotocol\b|"
                                  r"\babstract\b", ln))
    return False


def _marker_stubs(text: str, lang: str) -> list:
    marker, header = _MARKERS[lang], _HEADERS.get(lang)
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        if not marker.search(line) or line.lstrip().startswith(("//", "#", "*", "/*")):
            continue
        name, at = "", i
        for j in range(i, max(-1, i - 12), -1):   # nearest header above
            # on the marker's own line only the text BEFORE it can be a header
            # (`fun f() = TODO()`), never the `new X(` of the throw itself
            src = lines[j][:marker.search(line).start()] if j == i else lines[j]
            m = header.search(src) if header else None
            if m:
                cand = next((g for g in m.groups() if g), "")
                if cand and cand not in _NOT_NAMES:
                    name, at = cand, j
                    break
        if not name:
            continue
        if _ABSTRACT_LINE.search(lines[at]) or _in_abstract_type(lines, at):
            continue
        # the body is ONLY the marker: nothing but braces/blank between them
        between = "\n".join(lines[at + 1:i]).strip()
        between = re.sub(r"[{}\s]", "", between)
        if between and not between.startswith(("//", "#", "/*", "*")):
            continue
        out.append((name, i + 1, "is a not-implemented stub"))
    return out


# Per language: what the stub does when called, and how the language marks a
# method that is MEANT to have no body here (so a real design can say so and
# stop looking like unfinished work).
_HINTS = {
    "py": ("calling it raises NotImplementedError at runtime",
           "a base-class hook is marked with `@abc.abstractmethod` (class "
           "inherits `abc.ABC`) or declared on a `typing.Protocol`"),
    "js": ("calling it throws at runtime",
           "in TypeScript an override point is an `abstract` method in an "
           "`abstract class`, or a member of an `interface`"),
    "java": ("calling it throws at runtime",
             "an override point is an `abstract` method or an `interface` method"),
    "kotlin": ("`TODO()` compiles but throws NotImplementedError when called",
               "an override point is an `abstract` or `open` member, or an "
               "`interface` member"),
    "csharp": ("calling it throws NotImplementedException at runtime",
               "an override point is an `abstract` member or an `interface` member"),
    "go": ("it compiles, but panics when called",
           "a contract with no body belongs in an `interface` type"),
    "rust": ("`todo!()`/`unimplemented!()` compile but panic when called",
             "a required method with no body is declared in a `trait`"),
    "ruby": ("calling it raises NotImplementedError at runtime",
             "a hook subclasses must define is documented as such on the base class"),
    "php": ("calling it throws at runtime",
            "an override point is an `abstract` method or an `interface` method"),
    "swift": ("calling it crashes the program (`fatalError`)",
              "a requirement with no body is declared in a `protocol`"),
    "scala": ("`???` compiles but throws NotImplementedError when called",
              "a member with no body is declared `abstract` in a class or `trait`"),
    "cpp": ("calling it throws at runtime",
            "an override point is a pure virtual function (`virtual ... = 0`)"),
}


def hint(path: str) -> str:
    """One sentence for ``path``'s language: what the stub does at runtime and
    how a deliberate no-body method is written there; "" when unknown."""
    ext = os.path.splitext(path)[1].lower()
    lang = "py" if ext == ".py" else _LANG.get(ext)
    h = _HINTS.get(lang or "")
    if not h:
        return ""
    return (f"In this language {h[0]}; if it is meant as an override point, "
            f"{h[1]} instead.")


def find_stubs(path: str, text: str) -> "list | None":
    """``[(name, line, why)]`` for each stub function in ``text``; None when
    the language is not read here."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        return _py_stubs(text)
    if ext == ".pyi":
        return []
    lang = _LANG.get(ext)
    if not lang:
        return None
    return _marker_stubs(text, lang)


def new_stubs(path: str, before: "str | None", now: str) -> list:
    """Stubs in ``now`` that ``before`` (the file at the work's start; None =
    the file is new) did not already have, matched by function name."""
    cur = find_stubs(path, now) or []
    if not cur:
        return []
    old = {n for n, _, _ in (find_stubs(path, before) or [])} if before else set()
    return [s for s in cur if s[0] not in old]


__all__ = ["find_stubs", "hint", "new_stubs"]
