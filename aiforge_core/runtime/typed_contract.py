"""Signatures a request states for a typed language, checked against the code.

The Python contract check (:mod:`spec_contract`) reads code with ``ast``, so
a request for a Java project that wrote "The unit test will call: `new
SessionNotifier(String url, String token)` and `void
passwordChanged(String phone)`" was never checked: a real Java service run
built ``SessionNotifier(WebClient, String)`` returning ``Mono<Void>``,
the test could not compile (0/4), and the run took 40 minutes; OpenCode, which
followed the stated signature, got 2/4.

Claims, from backticked code in the request (Java / Kotlin / C# / Scala):
- ``new [pkg.]Name(Type a, Type b)``          — a constructor of Name;
- ``[RetType ]pkg.Name.method(Type a)`` or ``Name#method(...)`` — a method on Name;
- ``[static] RetType method(Type a)``         — a method (any class).
Parameter TYPES are compared (base names: no package, no generics), and a
stated return type. A claim with untyped arguments (a call example such as
``passwordChanged("9845")``) is not a signature and is skipped.
"""
from __future__ import annotations

import os
import re

_EXT = (".java", ".kt", ".scala", ".cs")
_MAX_FILES = 4000
_TICK = re.compile(r"`([^`\n]{3,300})`")
_NEW = re.compile(r"\bnew\s+(?:[\w$]+\.)*([A-Z][\w$]*)\s*\(([^()]*)\)")
_ON_CLASS = re.compile(
    r"(?:^|[\s(])(?:(?:static|public|private|protected|final)\s+)*(?:([\w$.<>\[\]?, ]+?)\s+)?"
    r"(?:[a-z_][\w$]*\.)*([A-Z][\w$]*)[.#]([a-z_][\w$]*)\s*\(([^()]*)\)")
_BARE = re.compile(
    r"(?:^|[\s(])(?:(?:static|public|private|protected|final|package-private)\s+)*"
    r"([A-Za-z_][\w$.]*(?:<[^()]*?>)?(?:\[\])*)\s+([a-z_][\w$]*)\s*\(([^()]*)\)")
_NOT_TYPES = {"new", "return", "the", "and", "or", "call", "calls", "with", "to", "a", "an"}


def _split(params: str) -> list:
    out, depth, cur = [], 0, ""
    for ch in params:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _base(t: str) -> str:
    t = re.sub(r"<.*>", "", t or "").replace("...", "[]").strip()
    t = t.split(".")[-1] if "(" not in t else t
    return t.replace("final ", "").strip()


def _types(params: str) -> "list | None":
    """Parameter base types, or None when the text is a call, not a signature."""
    out = []
    for p in _split(params):
        p = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", p).replace("final ", "").strip()
        if ":" in p and not p.startswith(("<", "?")):       # Kotlin / Scala: name: Type
            name, typ = p.split(":", 1)
            out.append(_base(typ)); continue
        words = re.findall(r"[\w$.<>\[\]?]+(?:<[^>]*>)?", p)
        if len(words) < 2 or not re.match(r"^[A-Za-z_]", words[0]):
            return None                                    # a value: a call example
        out.append(_base(" ".join(words[:-1])))
    return out


def claims(request: str) -> list:
    """``[(kind, cls, name, types, ret, text)]`` the request states."""
    out, seen = [], set()
    for m in _TICK.finditer(request or ""):
        span = m.group(1)
        for c in _NEW.finditer(span):
            ts = _types(c.group(2))
            if ts is not None:
                out.append(("ctor", c.group(1), c.group(1), ts, None, c.group(0).strip()))
        if _NEW.search(span):
            continue
        hit = False
        for c in _ON_CLASS.finditer(span):
            ts = _types(c.group(4))
            if ts is None:
                continue
            ret = _base(c.group(1).split()[-1]) if c.group(1) and c.group(1).split()[-1] not in _NOT_TYPES else None
            out.append(("method", c.group(2), c.group(3), ts, ret, c.group(0).strip()))
            hit = True
        if hit:
            continue
        for c in _BARE.finditer(span):
            ret = c.group(1)
            if ret in _NOT_TYPES:
                continue
            ts = _types(c.group(3))
            if ts is None:
                continue
            out.append(("method", None, c.group(2), ts, _base(ret), c.group(0).strip()))
    uniq = []
    for c in out:
        k = c[:5] if not isinstance(c[3], list) else (c[0], c[1], c[2], tuple(c[3]), c[4])
        if k not in seen:
            seen.add(k); uniq.append(c)
    return uniq


def _sources(root: str) -> list:
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in (
            "target", "build", "node_modules", "out", "bin", "obj")]
        for f in files:
            if f.endswith(_EXT):
                out.append(os.path.join(d, f))
                if len(out) >= _MAX_FILES:
                    return out
    return out


def _balanced(text: str, i: int) -> "tuple[str, int] | None":
    """The text inside the parentheses that open at ``text[i]``, and the index
    just past them (annotations such as ``@Value("${x}")`` nest parentheses)."""
    depth = 0
    for j in range(i, min(len(text), i + 4000)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
    return None


def _decls(text: str, cls: "str | None", name: str, ctor: bool) -> list:
    """``[(types, ret)]`` declared in ``text`` for ``name``."""
    found = []
    if ctor:
        for m in re.finditer(rf"\b(?:(?:record|class|data class)\s+)?{re.escape(name)}\s*(?=\()", text):
            got = _balanced(text, m.end())
            if not got:
                continue
            inner, end = got
            tail = text[end:end + 200].lstrip()
            header = bool(re.match(r"(?:record|class|data class)\s", m.group(0)))
            if not header and not re.match(r"(?:throws[^{;]*)?\{", tail):
                continue                                   # a call (`new Name(...)`), not a declaration
            if not header and re.search(r"\bnew\s*$", text[max(0, m.start() - 8):m.start()]):
                continue
            ts = _types(inner) if inner.strip() else []
            if ts is not None:
                found.append((ts, None))
        return found
    for m in re.finditer(rf"(?:\bfun\s+)?\b{re.escape(name)}\s*(?=\()", text):
        got = _balanced(text, m.end())
        if not got:
            continue
        inner, end = got
        tail = text[end:end + 300]
        before = text[max(0, m.start() - 200):m.start()]
        if m.group(0).startswith("fun"):                 # Kotlin: fun name(a: T): R
            r = re.match(r"\s*:\s*([\w$.<>?]+)", tail)
            ret = _base(r.group(1)) if r else "Unit"
        else:
            if not re.match(r"\s*(?:throws[^{;]*)?[{;]", tail):
                continue                                  # a call, not a declaration
            head = re.findall(r"[\w$.<>\[\]?]+(?:<[^<>]*>)?", before.splitlines()[-1] if before else "")
            if not head or head[-1] in ("new", "return", "else", "=", ".", "throw"):
                continue
            ret = _base(head[-1])
        ts = _types(inner) if inner.strip() else []
        if ts is not None:
            found.append((ts, ret))
    return found


def missing(request: str, root: str) -> list:
    """One line per stated signature the code does not declare as stated."""
    cl = claims(request)
    if not cl or not root or not os.path.isdir(root):
        return []
    files = _sources(root)
    if not files:
        return []
    texts: dict = {}

    def text(p):
        if p not in texts:
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    texts[p] = fh.read()
            except OSError:
                texts[p] = ""
        return texts[p]

    gaps = []
    for kind, cls, name, ts, ret, shown in cl:
        owner = cls if kind == "ctor" else cls
        cands = [p for p in files if owner and os.path.splitext(os.path.basename(p))[0] == owner]
        if not cands and owner:
            cands = [p for p in files if re.search(rf"\b(?:class|record|interface|object)\s+{re.escape(owner)}\b", text(p))]
        if not owner:
            cands = [p for p in files if re.search(rf"\b{re.escape(name)}\s*\(", text(p))
                     and "/test/" not in p.replace(os.sep, "/")]
        decls = [d for p in cands for d in _decls(text(p), cls, name, kind == "ctor")]
        if any(d[0] == ts and (ret is None or d[1] is None or d[1] == ret) for d in decls):
            continue
        if not decls:
            gaps.append(f"`{shown}` — not declared in the code")
        else:
            got = "; ".join(f"{name}({', '.join(d[0])})" + (f" returning {d[1]}" if d[1] and kind != "ctor" else "")
                            for d in decls[:3])
            gaps.append(f"`{shown}` — the code declares {got}")
    return gaps


__all__ = ["claims", "missing"]
