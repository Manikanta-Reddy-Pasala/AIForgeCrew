"""Stage 4 — tree-sitter walk.

For every source file under repo_path:
    - hash the bytes
    - parse with tree-sitter (per-language grammar via tree-sitter-language-pack)
    - run the language-specific query (`queries/<lang>.scm`)
    - emit File_v2 props + Symbol_v2 nodes + DEFINES + IMPORTS edges (lists)

This module returns dataclasses; the writer (`store/symbol_writer.py`)
upserts them into Neo4j. Stage 5 (edges.py) layers CALLS on top.

Languages supported in plan 3:
    .py    -> python
    .java  -> java
    .ts/.tsx -> typescript

Files in unsupported languages are emitted as bare File_v2 nodes
(hash + lang + lines, no symbols).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Query, QueryCursor
from tree_sitter_language_pack import get_language, get_parser

# Mapping file extension → tree-sitter language id
_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "tsx",   # TSX parser handles JSX trees
    ".mjs": "javascript",
    ".cjs": "javascript",
}

# Documentation extensions — no tree-sitter parse, but still walked +
# embedded so README/CLAUDE.md/ADRs/CHANGELOG end up in Chunk_v2 and
# vector search can hit them.
_DOC_EXT: dict[str, str] = {
    ".md":   "doc-md",
    ".rst":  "doc-rst",
    ".adoc": "doc-adoc",
    ".txt":  "doc-txt",
}

# Build-manifest filenames — useful metadata for "what depends on what"
# queries. Indexed as doc-manifest so vector search can surface them.
_MANIFEST_NAMES: frozenset[str] = frozenset({
    "pom.xml",
    "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts",
    "package.json", "package-lock.json",
    "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg",
    "cargo.toml", "go.mod", "go.sum",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "makefile", ".env.example",
})

# Skip these directories when walking — don't index build artifacts.
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "target", "build", "dist",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".idea", ".vscode", ".DS_Store",
    ".aiforge", ".aiforge-worktrees", "graphify-out",
}


@dataclass
class WalkedSymbol:
    fqname: str
    kind: str               # class | interface | enum | annotation | method | function | field
    file_path: str
    signature: str = ""
    doc_first_line: str = ""
    line_start: int = 0
    line_end: int = 0
    # Enrichment (best-effort; empty when language adapter can't infer)
    visibility: str = ""    # public | private | protected | package
    modifiers: list[str] = field(default_factory=list)
    return_type: str = ""
    params_json: str = ""   # JSON-encoded list[{"name", "type"}]
    deprecated: bool = False


@dataclass
class WalkedFile:
    repo: str
    path: str               # repo-relative
    hash: str
    lang: str
    lines: int
    symbols: list[WalkedSymbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    parse_error: bool = False


def lang_for(path: str | Path) -> str | None:
    p = Path(path)
    suf = p.suffix.lower()
    if p.name.lower() in _MANIFEST_NAMES:
        return "doc-manifest"
    return _EXT_LANG.get(suf) or _DOC_EXT.get(suf)


def is_doc(path: str | Path) -> bool:
    p = Path(path)
    return (
        p.suffix.lower() in _DOC_EXT
        or p.name.lower() in _MANIFEST_NAMES
    )


def _gitignored_paths(root: Path) -> set[str]:
    """Use `git ls-files` to enumerate IGNORED paths under root.

    Returns a set of repo-relative paths git would skip. Empty set if
    the dir isn't a git repo or git CLI fails.
    Honors .gitignore + global excludes natively — no Python-side
    pathspec parsing required.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["git", "ls-files", "--others", "--ignored",
             "--exclude-standard", "-z"],
            cwd=str(root), capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if r.returncode != 0:
        return set()
    out = r.stdout.decode("utf-8", "replace") if r.stdout else ""
    return {p for p in out.split("\0") if p}


def walk_repo(repo_path: str | Path, *, repo: str) -> list[WalkedFile]:
    repo_path = Path(repo_path).resolve()
    ignored = _gitignored_paths(repo_path)
    out: list[WalkedFile] = []
    for path in _iter_source_files(repo_path):
        rel = str(path.relative_to(repo_path))
        # Honor .gitignore — drop paths git considers ignored.
        if rel in ignored:
            continue
        p = Path(rel)
        suf = p.suffix.lower()
        # Manifest files matched by basename, code/docs by suffix.
        if p.name.lower() in _MANIFEST_NAMES:
            lang = "doc-manifest"
        else:
            lang = _EXT_LANG.get(suf) or _DOC_EXT.get(suf)
        try:
            data = path.read_bytes()
        except (OSError, ValueError):
            continue
        sha = hashlib.sha256(data).hexdigest()
        lines = data.count(b"\n") + 1
        wf = WalkedFile(repo=repo, path=rel, hash=sha,
                        lang=lang or "other", lines=lines)
        # Code → tree-sitter parse for symbols/imports.
        # Docs / manifests → no parse; just walked.
        if suf in _EXT_LANG:
            try:
                _parse_into(wf, data, lang)
            except Exception:
                wf.parse_error = True
        out.append(wf)
    return out


def _iter_source_files(root: Path):
    for p in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if (suf in _EXT_LANG
                or suf in _DOC_EXT
                or p.name.lower() in _MANIFEST_NAMES):
            yield p


# Every def pattern in the .scm queries captures a `<kind>.def` + `<kind>.name`
# pair, so the nine-branch if/elif chain that used to read them was the same
# three lines written out nine times. The kinds are data now; the code is one
# loop. Order matters: it is the order the old chain tested in.
_DEF_KINDS = ("class", "interface", "enum", "annotation",
              "field", "method", "function")
# Types that can OWN a method or field — the ones whose line ranges become the
# ownership index.
_TYPE_KINDS = ("class", "interface", "enum", "annotation")
_IMPORT_CAPTURES = ("import.module", "import.from")


def _match_defs(caps: dict, source: bytes) -> tuple[str, list[tuple[str, object]]]:
    """``(kind, [(name, def_node)])`` for the FIRST def/name pair in one match.

    First, not all: the original chain was if/elif, so a match that somehow
    carried two kinds counted as the earlier one only.
    """
    for kind in _DEF_KINDS:
        defs, names = caps.get(f"{kind}.def"), caps.get(f"{kind}.name")
        if defs and names:
            return kind, [(_text(n, source), d) for d, n in zip(defs, names)]
    return "", []


def _match_imports(caps: dict, source: bytes) -> list[str]:
    """The import strings in one match — again, the first capture that hit."""
    for cap in _IMPORT_CAPTURES:
        nodes = caps.get(cap)
        if nodes:
            return [_text(n, source) for n in nodes]
    return []


def _collect(cursor, root, source: bytes) -> tuple[dict[str, list], list[str]]:
    """One query pass → ``{kind: [(name, node)]}`` plus the import strings.

    `matches()` preserves per-pattern groupings, so name + def of the same match
    always belong to the same node — robust against the capture-ordering quirks
    of `captures()`.
    """
    buckets: dict[str, list[tuple[str, object]]] = {k: [] for k in _DEF_KINDS}
    imports: list[str] = []
    for _match_id, caps in cursor.matches(root):
        kind, found = _match_defs(caps, source)
        if kind:
            buckets[kind] += found
        else:
            imports += _match_imports(caps, source)
    return buckets, imports


def _add_type_symbols(wf: WalkedFile, buckets: dict,
                      source: bytes) -> list[tuple[int, int, str]]:
    """Record the type symbols; return their line ranges for ownership lookup."""
    ranges: list[tuple[int, int, str]] = []
    for kind in _TYPE_KINDS:
        for name, node in buckets[kind]:
            ranges.append((node.start_point[0], node.end_point[0], name))
            wf.symbols.append(_make_symbol(
                wf=wf, name=name, kind=kind, node=node, source=source,
            ))
    return ranges


def _add_member_symbols(wf: WalkedFile, buckets: dict,
                        ranges: list[tuple[int, int, str]],
                        source: bytes) -> None:
    """Methods and fields (qualified by their owner), then free functions."""
    for kind in ("method", "field"):
        for name, node in buckets[kind]:
            owner = _enclosing_class(ranges, node.start_point[0])
            wf.symbols.append(_make_symbol(
                wf=wf, name=name, kind=kind, node=node, source=source,
                fqname=_fqname(wf.path, name, parent_class=owner),
            ))
    for name, node in buckets["function"]:
        # A "function" inside a type is a method, already recorded above.
        if _enclosing_class(ranges, node.start_point[0]):
            continue
        wf.symbols.append(_make_symbol(
            wf=wf, name=name, kind="function", node=node, source=source,
        ))


def _parse_into(wf: WalkedFile, source: bytes, lang: str) -> None:
    parser = get_parser(lang)
    language = get_language(lang)
    tree = parser.parse(source)
    query_text = _load_query(lang)
    if not query_text:
        return
    cursor = QueryCursor(Query(language, query_text))

    buckets, imports = _collect(cursor, tree.root_node, source)
    ranges = _add_type_symbols(wf, buckets, source)
    _add_member_symbols(wf, buckets, ranges, source)
    wf.imports = list(dict.fromkeys(imports))   # de-dup, preserve order


def _enclosing_class(
    class_ranges: list[tuple[int, int, str]], line: int,
) -> str | None:
    for start, end, name in class_ranges:
        if start <= line <= end:
            return name
    return None


def _fqname(file_path: str, name: str, *, parent_class: str | None = None) -> str:
    if parent_class:
        return f"{file_path}::{parent_class}::{name}"
    return f"{file_path}::{name}"


def _make_symbol(
    *, wf: WalkedFile, name: str, kind: str, node, source: bytes,
    fqname: str | None = None,
) -> WalkedSymbol:
    sig_line = source.split(b"\n")[node.start_point[0]].decode(
        "utf-8", errors="replace"
    ).strip()
    sym = WalkedSymbol(
        fqname=fqname or _fqname(wf.path, name),
        kind=kind,
        file_path=wf.path,
        signature=sig_line[:200],
        doc_first_line="",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
    )
    try:
        _enrich_symbol(sym, node=node, source=source, lang=wf.lang)
    except Exception:  # noqa: BLE001 — enrichment is best-effort
        pass
    return sym


# ─── Symbol enrichment ────────────────────────────────────────────────
# Walks the def-node's children to extract visibility, modifiers, return
# type, parameters, and @Deprecated / @deprecated. Per-language rules
# kept narrow: when in doubt, leave the field empty rather than guess.

_JAVA_VISIBILITY = {"public", "private", "protected"}
_JAVA_MODIFIERS = {
    "static", "final", "abstract", "synchronized", "native",
    "transient", "volatile", "default", "strictfp",
}


def _enrich_symbol(sym: WalkedSymbol, *, node, source: bytes, lang: str) -> None:
    if lang == "java":
        _enrich_java(sym, node=node, source=source)
    elif lang == "python":
        _enrich_python(sym, node=node, source=source)
    elif lang in ("typescript", "tsx", "javascript"):
        _enrich_ts(sym, node=node, source=source)


def _java_modifiers(modifiers_node, source: bytes) -> tuple[list[str], list[str], bool]:
    """``(visibility_words, other_modifiers, deprecated)`` from a modifiers node."""
    seen_vis: list[str] = []
    mods: list[str] = []
    deprecated = False
    for child in _walk_children(modifiers_node):
        text = _text(child, source)
        if child.type in ("marker_annotation", "annotation"):
            deprecated = deprecated or "Deprecated" in text
        elif text in _JAVA_VISIBILITY:
            seen_vis.append(text)
        elif text in _JAVA_MODIFIERS:
            mods.append(text)
    return seen_vis, mods, deprecated


def _java_params(params_node, source: bytes) -> list[dict]:
    """``[{"name", "type"}]`` for every formal parameter under ``params_node``."""
    out: list[dict] = []
    for child in _walk_children(params_node):
        if child.type != "formal_parameter":
            continue
        pname = _child_by_field(child, "name")
        ptype = _child_by_field(child, "type")
        out.append({
            "name": _text(pname, source) if pname else "",
            "type": _text(ptype, source) if ptype else "",
        })
    return out


def _enrich_java(sym: WalkedSymbol, *, node, source: bytes) -> None:
    """For class/interface/method/field/constructor nodes: read the
    `modifiers` child if present, plus `type` (return) and
    `formal_parameters` for method-like nodes.

    The two inner walks live in their own functions: reading modifiers and
    reading a parameter list are different questions, and inline they shared a
    stack of four `if`s that had nothing to do with each other.
    """
    import json as _json

    modifiers_node = _child_by_field(node, "modifiers") or _first_child_of_type(
        node, "modifiers"
    )
    if modifiers_node is not None:
        seen_vis, mods, deprecated = _java_modifiers(modifiers_node, source)
        if deprecated:
            sym.deprecated = True
        # No explicit modifier means package-private in Java — the one default
        # that is not "public".
        sym.visibility = seen_vis[0] if seen_vis else "package"
        sym.modifiers = mods

    if sym.kind == "method":
        rt = _child_by_field(node, "type")
        if rt is not None:
            sym.return_type = _text(rt, source).strip()
        params = _child_by_field(node, "parameters")
        out = _java_params(params, source) if params is not None else []
        if out:
            sym.params_json = _json.dumps(out, separators=(",", ":"))


def _python_visibility(fqname: str) -> str:
    """Python's naming convention, as a visibility: ``__x`` private, ``_x``
    protected, anything else public (``__dunder__`` is public)."""
    name = fqname.rsplit("::", 1)[-1]
    if name.startswith("__") and not name.endswith("__"):
        return "private"
    if name.startswith("_"):
        return "protected"
    return "public"


def _python_deprecated(node, source: bytes) -> bool:
    """True when a decorator on this definition mentions "deprecated"."""
    parent = node.parent
    if parent is None or parent.type != "decorated_definition":
        return False
    return any(child.type == "decorator"
               and "deprecated" in _text(child, source).lower()
               for child in _walk_children(parent))


def _enrich_python(sym: WalkedSymbol, *, node, source: bytes) -> None:
    """Python: visibility derived from name (`_x` private convention),
    return-type annotation, parameters with type hints, @deprecated."""
    import json as _json

    sym.visibility = _python_visibility(sym.fqname)
    if _python_deprecated(node, source):
        sym.deprecated = True

    if sym.kind in ("method", "function"):
        rt = _child_by_field(node, "return_type")
        if rt is not None:
            sym.return_type = _text(rt, source).strip()
        params = _child_by_field(node, "parameters")
        out: list[dict] = []
        if params is not None:
            for child in _walk_children(params):
                pname, ptype = _python_param(child, source)
                if pname:
                    out.append({"name": pname, "type": ptype})
        if out:
            sym.params_json = _json.dumps(out, separators=(",", ":"))


def _py_param_identifier(child, source: bytes) -> tuple[str, str]:
    return _text(child, source), ""


def _py_param_typed(child, source: bytes) -> tuple[str, str]:
    """``typed_parameter``: first identifier child is the name, field "type"
    is the annotation."""
    ident = _first_child_of_type(child, "identifier")
    if ident is None:
        return "", ""
    type_node = _child_by_field(child, "type")
    return _text(ident, source), _text(type_node, source) if type_node else ""


def _py_param_default(child, source: bytes) -> tuple[str, str]:
    """``default_parameter`` / ``typed_default_parameter``.

    Field "name" works for the plain variant; for the typed one we descend
    into the typed_parameter sub-tree.
    """
    name_node = _child_by_field(child, "name")
    if name_node is not None:
        type_node = _child_by_field(child, "type")
        return (_text(name_node, source),
                _text(type_node, source) if type_node else "")
    for sub in _walk_children(child):
        if sub.type == "typed_parameter":
            return _py_param_typed(sub, source)
    return "", ""


def _py_param_splat(child, source: bytes) -> tuple[str, str]:
    """``*args`` / ``**kwargs`` — the star is part of the name."""
    ident = _first_child_of_type(child, "identifier")
    prefix = "*" if child.type == "list_splat_pattern" else "**"
    return (prefix + _text(ident, source) if ident else ""), ""


# One handler per tree-sitter parameter node type. A dispatch table rather than
# a five-branch if/elif: each shape is read in isolation, and adding one is a
# row here instead of another level of nesting.
_PY_PARAM_HANDLERS = {
    "identifier": _py_param_identifier,
    "typed_parameter": _py_param_typed,
    "default_parameter": _py_param_default,
    "typed_default_parameter": _py_param_default,
    "list_splat_pattern": _py_param_splat,
    "dictionary_splat_pattern": _py_param_splat,
}


def _python_param(child, source: bytes) -> tuple[str, str]:
    """Best-effort: extract (name, type) from one Python parameter node.
    Handles `identifier`, `typed_parameter`, `default_parameter`,
    `typed_default_parameter`, and `(*args, **kwargs)`. Anything else is
    ``("", "")`` — the caller drops nameless parameters."""
    handler = _PY_PARAM_HANDLERS.get(child.type)
    return handler(child, source) if handler else ("", "")


def _enrich_ts(sym: WalkedSymbol, *, node, source: bytes) -> None:
    """TS/JS: best-effort visibility from `accessibility_modifier`,
    `static` modifier from `readonly`/`static` siblings, return type
    when annotated. Anonymous + arrow functions are skipped — their
    metadata lives at the assignment site, not on the function node."""
    # accessibility_modifier appears as a child of class members
    for child in _walk_children(node):
        if child.type == "accessibility_modifier":
            sym.visibility = _text(child, source).strip()
        elif child.type == "static":
            sym.modifiers.append("static")
    if not sym.visibility:
        sym.visibility = "public"
    rt = _child_by_field(node, "return_type")
    if rt is not None:
        sym.return_type = _text(rt, source).strip().lstrip(":").strip()


# ─── tree-sitter node helpers ─────────────────────────────────────────

def _child_by_field(node, field_name: str):
    """Return node's named child for `field_name`, or None."""
    try:
        return node.child_by_field_name(field_name)
    except Exception:  # noqa: BLE001
        return None


def _first_child_of_type(node, type_name: str):
    for child in _walk_children(node):
        if child.type == type_name:
            return child
    return None


def _walk_children(node):
    """Yield direct named + anonymous children. Avoids cursor allocations."""
    for i in range(node.child_count):
        yield node.child(i)


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _load_query(lang: str) -> str:
    """Resolve language id → query file. tsx maps to typescript (JSX
    trees are a superset of TS); javascript has its own dedicated
    query so JS files yield real symbols (the prior fallback to the
    typescript query matched zero patterns on JS trees because TS-only
    node types like `interface_declaration` / `type_identifier` don't
    appear there)."""
    qfile = Path(__file__).parent / "queries" / f"{lang}.scm"
    if qfile.is_file():
        return qfile.read_text()
    if lang == "tsx":
        qfile = Path(__file__).parent / "queries" / "typescript.scm"
        return qfile.read_text() if qfile.is_file() else ""
    return ""
