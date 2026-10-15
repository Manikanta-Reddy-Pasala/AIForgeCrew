"""A source file's outline — its classes, functions and methods with line
ranges — read with tree-sitter, so a big file can be shown as a map instead
of 80,000 characters of text.

A whole large file in the prompt fills the context, and a file cut at a size
cap leaves the model guessing what the rest says. The outline is a few hundred
characters per file; the model then reads only the symbol or the line range it
needs (``file_read`` with ``symbol``, or ``read_lines``).

The tag queries are the vendored RepoMap's (``indexing/repomap/queries``), so
every language the repo map knows has an outline. A file with no grammar, or
one that fails to parse, has none: callers fall back to the text. Never raises.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: A definition shorter than this many lines is not worth its own outline row
#: when the file has many symbols; classes are always kept.
_MAX_ROWS = 400


@dataclass(frozen=True)
class Symbol:
    kind: str        # class / function / method / interface / …
    name: str
    start: int       # 1-based, inclusive
    end: int         # 1-based, inclusive
    depth: int       # nesting: a method in a class is 1
    signature: str   # the definition's first source line, trimmed


def _language(path: str):
    from grep_ast import filename_to_lang
    from grep_ast.tsl import get_language, get_parser
    lang = filename_to_lang(path)
    if not lang:
        return None
    from aiforge_core.indexing.repomap._vendor_repomap import get_scm_fname
    scm = get_scm_fname(lang)
    if not scm or not scm.exists():
        return None
    return get_language(lang), get_parser(lang), scm.read_text()


def _captures(language, query_text: str, root):
    from tree_sitter import Query
    query = Query(language, query_text)
    if hasattr(query, "captures"):
        return query.captures(root)
    from tree_sitter import QueryCursor
    return QueryCursor(query).captures(root)


def _definitions(language, query_text: str, root) -> list[tuple]:
    """``(kind, name, node)`` per definition, the node spanning its body."""
    caps = _captures(language, query_text, root)
    if isinstance(caps, dict):
        pairs = [(node, tag) for tag, nodes in caps.items() for node in nodes]
    else:
        pairs = list(caps)
    whole = {}
    names = []
    for node, tag in pairs:
        if tag.startswith("definition."):
            whole[(node.start_byte, node.end_byte)] = node
        elif tag.startswith("name.definition."):
            names.append((tag[len("name.definition."):], node))
    out = []
    seen = set()
    for kind, name_node in names:
        # the nearest enclosing definition node: a walk up the tree, not a
        # scan of every definition (a generated C file has thousands)
        body = name_node
        while body is not None and (body.start_byte, body.end_byte) not in whole:
            body = body.parent
        body = _widen(body or name_node.parent or name_node)
        key = (body.start_byte, body.end_byte)
        if key in seen:
            continue
        seen.add(key)
        out.append((kind, name_node.text.decode("utf-8", "replace"), body))
    return out


#: C and C++ tag a function by its declarator (the signature line only):
#: the definition around it holds the body.
_DECLARATOR_PARENTS = ("function_definition", "declaration", "field_declaration",
                       "template_declaration")


#: Wrappers that belong to the definition: Python decorators, a TS/JS
#: ``export``. A symbol read without them loses ``@property`` / ``export``.
_WRAPPERS = ("decorated_definition", "export_statement")


def _widen(node):
    if node is not None and node.type == "function_declarator":
        up = node.parent
        while up is not None and up.type not in _DECLARATOR_PARENTS:
            up = up.parent
        node = up or node
    while node is not None and node.parent is not None \
            and node.parent.type in _WRAPPERS:
        node = node.parent
    return node


def split_lines(text: str) -> list[str]:
    """Lines as tree-sitter and ``read_lines`` count them: on "\n" only (a
    form feed or U+2028 inside a line is not a break), no phantom last line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def outline(path: str, text: str | None = None) -> list[Symbol] | None:
    """The symbols of ``path`` in file order, or ``None`` when the language has
    no grammar or the file cannot be parsed."""
    try:
        found = _language(path)
        if not found:
            return None
        language, parser, query_text = found
        if text is None:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        source = text.encode("utf-8")
        tree = parser.parse(source)
        defs = _definitions(language, query_text, tree.root_node)
    except Exception:  # noqa: BLE001 — no outline, the caller shows text
        return None
    lines = split_lines(text)
    defs.sort(key=lambda d: (d[2].start_byte, -d[2].end_byte))
    stack: list[int] = []
    symbols = []
    for kind, name, node in defs:
        while stack and node.start_byte >= stack[-1]:
            stack.pop()
        start = node.start_point[0] + 1
        end = max(start, node.end_point[0] + 1)
        first = lines[start - 1].strip() if start - 1 < len(lines) else name
        if kind == "function" and stack:
            kind = "method"
        symbols.append(Symbol(kind, name, start, end, len(stack), first[:160]))
        stack.append(node.end_byte)
    return symbols


def render(path: str, symbols: list[Symbol], total_lines: int) -> str:
    """The outline as text: one row per symbol, ``start-end  signature``."""
    rows = symbols
    if len(rows) > _MAX_ROWS:
        rows = [s for s in symbols if s.depth == 0 or s.end - s.start >= 5][:_MAX_ROWS]
    width = len(str(total_lines))
    out = []
    for s in rows:
        span = f"{s.start:>{width}}-{s.end:<{width}}"
        out.append(f"{span}  {'  ' * s.depth}{s.signature}")
    if len(rows) < len(symbols):
        out.append(f"… {len(symbols) - len(rows)} small symbols not listed")
    return "\n".join(out)


def find_symbol(symbols: list[Symbol], wanted: str) -> list[Symbol]:
    """Symbols named ``wanted``. ``Class.method`` picks the method inside that
    class; a bare name matches at any depth."""
    wanted = (wanted or "").strip()
    if not wanted:
        return []
    parts = [p for p in wanted.replace("::", ".").split(".") if p]
    leaf = parts[-1]
    hits = [s for s in symbols if s.name == leaf]
    if len(parts) > 1:
        # Class.method means that class: another class's method of the same
        # name is not it (the caller shows the outline instead).
        owner = parts[-2]
        parents = [s for s in symbols if s.name == owner]
        inside = [h for h in hits
                  if any(p.start <= h.start and h.end <= p.end and p is not h
                         for p in parents)]
        # Go receivers, Rust impl blocks and C++ out-of-line definitions
        # (`func (s *Server) Start`, `impl Server`, `void Server::start`) put
        # the method outside the type: its own line names the owner.
        named = [h for h in hits if _names_owner(h, owner)]
        container = any(p.start <= s.start and s.end <= p.end and s is not p
                        for p in parents for s in symbols)
        # A type that holds no symbols (a Rust struct with an `impl` block
        # elsewhere, a Go struct) cannot say which method is its own: the
        # bare name is the best answer there, never in a real container.
        both = inside + [h for h in named if h not in inside]
        loose = [h for h in hits if not _owned_elsewhere(h, owner, symbols)]
        hits = both or ([] if container else loose)
    # a definition with a body before a one-line prototype of the same name
    return sorted(hits, key=lambda s: (s.end == s.start, s.start))


_OTHER_OWNER = __import__("re").compile(
    r"(\w+)\s*::\s*~?\w+\s*\(|^\s*func\s*\(\s*\w*\s*\*?\s*(\w+)")


def _owned_elsewhere(sym: Symbol, owner: str, symbols: list[Symbol]) -> bool:
    """``sym`` belongs to a type other than ``owner``: it sits inside another
    symbol, or its own line names another owner (``Client::start``, a Go
    receiver ``(c *Client)``)."""
    if any(p.start <= sym.start and sym.end <= p.end and p is not sym
           and p.name != owner for p in symbols):
        return True
    m = _OTHER_OWNER.search(sym.signature)
    named = m and (m.group(1) or m.group(2))
    return bool(named and named != owner)


def _names_owner(sym: Symbol, owner: str) -> bool:
    import re
    sig = sym.signature
    return bool(re.search(rf"\b{re.escape(owner)}\s*::\s*{re.escape(sym.name)}\b", sig)
                or (sym.kind in ("method", "function")
                    and re.search(rf"^\s*func\s*\([^)]*\b{re.escape(owner)}\b", sig)))


def is_source(path: str) -> bool:
    """A file the outline can read (has a grammar and a tag query)."""
    try:
        return _language(path) is not None
    except Exception:  # noqa: BLE001
        return False


def big_file_chars() -> int:
    """A source file longer than this (chars) is read as an outline unless the
    caller asks for all of it. ``AIFORGE_CHAT_READ_OUTLINE_CHARS``; 0 = off."""
    try:
        return max(0, int(os.environ.get("AIFORGE_CHAT_READ_OUTLINE_CHARS", "24000")))
    except ValueError:
        return 24000
