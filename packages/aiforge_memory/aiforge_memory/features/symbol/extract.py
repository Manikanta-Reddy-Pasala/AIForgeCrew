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


def _parse_into(wf: WalkedFile, source: bytes, lang: str) -> None:
    parser = get_parser(lang)
    language = get_language(lang)
    tree = parser.parse(source)
    query_text = _load_query(lang)
    if not query_text:
        return
    query = Query(language, query_text)
    cursor = QueryCursor(query)

    classes: list[tuple[str, object]] = []
    interfaces: list[tuple[str, object]] = []
    enums: list[tuple[str, object]] = []
    annotations: list[tuple[str, object]] = []
    fields: list[tuple[str, object]] = []
    methods: list[tuple[str, object]] = []
    functions: list[tuple[str, object]] = []
    imports: list[str] = []

    # `matches()` preserves per-pattern groupings, so name + def of the
    # same match always belong to the same node — robust against the
    # capture-ordering quirks of `captures()`.
    for _match_id, caps in cursor.matches(tree.root_node):
        # caps: dict[capture_name, list[Node]]
        if "class.def" in caps and "class.name" in caps:
            for d, n in zip(caps["class.def"], caps["class.name"]):
                classes.append((_text(n, source), d))
        elif "interface.def" in caps and "interface.name" in caps:
            for d, n in zip(caps["interface.def"], caps["interface.name"]):
                interfaces.append((_text(n, source), d))
        elif "enum.def" in caps and "enum.name" in caps:
            for d, n in zip(caps["enum.def"], caps["enum.name"]):
                enums.append((_text(n, source), d))
        elif "annotation.def" in caps and "annotation.name" in caps:
            for d, n in zip(caps["annotation.def"], caps["annotation.name"]):
                annotations.append((_text(n, source), d))
        elif "field.def" in caps and "field.name" in caps:
            for d, n in zip(caps["field.def"], caps["field.name"]):
                fields.append((_text(n, source), d))
        elif "method.def" in caps and "method.name" in caps:
            for d, n in zip(caps["method.def"], caps["method.name"]):
                methods.append((_text(n, source), d))
        elif "function.def" in caps and "function.name" in caps:
            for d, n in zip(caps["function.def"], caps["function.name"]):
                functions.append((_text(n, source), d))
        elif "import.module" in caps:
            for n in caps["import.module"]:
                imports.append(_text(n, source))
        elif "import.from" in caps:
            for n in caps["import.from"]:
                imports.append(_text(n, source))

    # Build owning-type index (class | interface | enum | annotation)
    # by line ranges so we can attach methods/fields back to their owner.
    type_ranges: list[tuple[int, int, str]] = []
    for cname, cdef in classes:
        type_ranges.append((cdef.start_point[0], cdef.end_point[0], cname))
        wf.symbols.append(_make_symbol(
            wf=wf, name=cname, kind="class", node=cdef, source=source,
        ))
    for iname, idef in interfaces:
        type_ranges.append((idef.start_point[0], idef.end_point[0], iname))
        wf.symbols.append(_make_symbol(
            wf=wf, name=iname, kind="interface", node=idef, source=source,
        ))
    for ename, edef in enums:
        type_ranges.append((edef.start_point[0], edef.end_point[0], ename))
        wf.symbols.append(_make_symbol(
            wf=wf, name=ename, kind="enum", node=edef, source=source,
        ))
    for aname, adef in annotations:
        type_ranges.append((adef.start_point[0], adef.end_point[0], aname))
        wf.symbols.append(_make_symbol(
            wf=wf, name=aname, kind="annotation", node=adef, source=source,
        ))
    for mname, mdef in methods:
        owner = _enclosing_class(type_ranges, mdef.start_point[0])
        fqname = _fqname(wf.path, mname, parent_class=owner)
        wf.symbols.append(_make_symbol(
            wf=wf, name=mname, kind="method", node=mdef, source=source,
            fqname=fqname,
        ))
    for fname_, fdef_ in fields:
        owner = _enclosing_class(type_ranges, fdef_.start_point[0])
        fqname = _fqname(wf.path, fname_, parent_class=owner)
        wf.symbols.append(_make_symbol(
            wf=wf, name=fname_, kind="field", node=fdef_, source=source,
            fqname=fqname,
        ))
    for fname, fdef in functions:
        # Skip if it's actually a method (already handled)
        if _enclosing_class(type_ranges, fdef.start_point[0]):
            continue
        wf.symbols.append(_make_symbol(
            wf=wf, name=fname, kind="function", node=fdef, source=source,
        ))

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


def _enrich_java(sym: WalkedSymbol, *, node, source: bytes) -> None:
    """For class/interface/method/field/constructor nodes: read the
    `modifiers` child if present, plus `type` (return) and
    `formal_parameters` for method-like nodes."""
    import json as _json

    modifiers_node = _child_by_field(node, "modifiers") or _first_child_of_type(
        node, "modifiers"
    )
    if modifiers_node is not None:
        seen_vis: list[str] = []
        mods: list[str] = []
        for child in _walk_children(modifiers_node):
            ttype = child.type
            text = _text(child, source)
            if ttype == "marker_annotation" or ttype == "annotation":
                if "Deprecated" in text:
                    sym.deprecated = True
                continue
            if text in _JAVA_VISIBILITY:
                seen_vis.append(text)
            elif text in _JAVA_MODIFIERS:
                mods.append(text)
        if seen_vis:
            sym.visibility = seen_vis[0]
        else:
            sym.visibility = "package"  # Java default
        sym.modifiers = mods

    if sym.kind in ("method",):
        rt = _child_by_field(node, "type")
        if rt is not None:
            sym.return_type = _text(rt, source).strip()
        params = _child_by_field(node, "parameters")
        if params is not None:
            out: list[dict] = []
            for child in _walk_children(params):
                if child.type == "formal_parameter":
                    pname = _child_by_field(child, "name")
                    ptype = _child_by_field(child, "type")
                    out.append({
                        "name": _text(pname, source) if pname else "",
                        "type": _text(ptype, source) if ptype else "",
                    })
            if out:
                sym.params_json = _json.dumps(out, separators=(",", ":"))


def _enrich_python(sym: WalkedSymbol, *, node, source: bytes) -> None:
    """Python: visibility derived from name (`_x` private convention),
    return-type annotation, parameters with type hints, @deprecated."""
    import json as _json

    # Name-based visibility convention
    name = sym.fqname.rsplit("::", 1)[-1]
    if name.startswith("__") and not name.endswith("__"):
        sym.visibility = "private"
    elif name.startswith("_"):
        sym.visibility = "protected"
    else:
        sym.visibility = "public"

    # Decorator inspection — walk parent's decorated_definition if any
    parent = node.parent
    if parent is not None and parent.type == "decorated_definition":
        for child in _walk_children(parent):
            if child.type == "decorator" and "deprecated" in _text(
                child, source
            ).lower():
                sym.deprecated = True

    if sym.kind in ("method", "function"):
        rt = _child_by_field(node, "return_type")
        if rt is not None:
            sym.return_type = _text(rt, source).strip()
        params = _child_by_field(node, "parameters")
        if params is not None:
            out: list[dict] = []
            for child in _walk_children(params):
                pname, ptype = _python_param(child, source)
                if pname:
                    out.append({"name": pname, "type": ptype})
            if out:
                sym.params_json = _json.dumps(out, separators=(",", ":"))


def _python_param(child, source: bytes) -> tuple[str, str]:
    """Best-effort: extract (name, type) from one Python parameter node.
    Handles `identifier`, `typed_parameter`, `default_parameter`,
    `typed_default_parameter`, and `(*args, **kwargs)`."""
    ttype = child.type
    if ttype == "identifier":
        return _text(child, source), ""
    if ttype == "typed_parameter":
        # First identifier child = name; field "type" = annotation.
        ident = _first_child_of_type(child, "identifier")
        type_node = _child_by_field(child, "type")
        if ident is None:
            return "", ""
        return (
            _text(ident, source),
            _text(type_node, source) if type_node else "",
        )
    if ttype in ("default_parameter", "typed_default_parameter"):
        # Field "name" works for default_parameter; for the typed
        # variant we descend into the typed_parameter sub-tree.
        name_node = _child_by_field(child, "name")
        if name_node is not None:
            type_node = _child_by_field(child, "type")
            return (
                _text(name_node, source),
                _text(type_node, source) if type_node else "",
            )
        # typed_default_parameter: first child is the typed_parameter
        for sub in _walk_children(child):
            if sub.type == "typed_parameter":
                return _python_param(sub, source)
    if ttype in ("list_splat_pattern", "dictionary_splat_pattern"):
        ident = _first_child_of_type(child, "identifier")
        prefix = "*" if ttype == "list_splat_pattern" else "**"
        return (prefix + _text(ident, source) if ident else "", "")
    return "", ""


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
