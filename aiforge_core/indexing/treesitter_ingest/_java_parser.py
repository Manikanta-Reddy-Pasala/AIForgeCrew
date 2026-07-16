"""Rich hand-written tree-sitter walker for Java (split from the original
``treesitter_ingest`` module — verbatim move, no behaviour change)."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ._models import FileParseResult, FileRecord, SymbolRecord
from ._setup import JAVA_PARSER, LARGE_FILE_LINE_THRESHOLD, TREESITTER_AVAILABLE


# ─────────────── tree-sitter helpers ───────────────

def _node_text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_all(node, kind: str) -> Iterable:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            yield n
        stack.extend(reversed(n.children))


def _find_child(node, kind: str):
    for c in node.children:
        if c.type == kind:
            return c
    return None


def _modifier_strings(src: bytes, decl) -> list[str]:
    mods = _find_child(decl, "modifiers")
    if mods is None:
        return []
    out: list[str] = []
    for a in mods.children:
        if a.type in ("annotation", "marker_annotation"):
            text = _node_text(src, a).strip()
            out.append(text.split("(", 1)[0])
        elif a.type == "modifier":
            out.append(_node_text(src, a))
        elif a.type in ("public", "private", "protected", "static", "final",
                        "abstract", "synchronized", "native", "default"):
            out.append(a.type)
    return out


# ─────────────── parsing ───────────────

_TYPE_NODE_KINDS = (
    "type_identifier", "generic_type", "void_type", "integral_type",
    "floating_point_type", "boolean_type", "scoped_type_identifier",
    "array_type",
)


def _extract_type(src: bytes, decl) -> str:
    for c in decl.children:
        if c.type in _TYPE_NODE_KINDS:
            return _node_text(src, c).split("\n", 1)[0][:200]
    return ""


def _parse_java_file(path: Path, src: bytes, repo: str, sha1: str) -> FileParseResult:
    if not TREESITTER_AVAILABLE:
        raise RuntimeError(
            "tree-sitter Java grammar unavailable — install `tree-sitter` + "
            "`tree-sitter-java` (they are declared deps; run `uv sync`)")
    tree = JAVA_PARSER.parse(src)
    root = tree.root_node

    pkg = ""
    imports: list[str] = []
    for n in root.children:
        if n.type == "package_declaration":
            pkg = _node_text(src, n).replace("package", "", 1).rstrip(";").strip()
        elif n.type == "import_declaration":
            txt = _node_text(src, n).replace("import", "", 1).rstrip(";").strip()
            txt = txt.replace("static ", "", 1).strip()
            if txt:
                imports.append(txt)

    loc = root.end_point[0] - root.start_point[0] + 1
    file_rec = FileRecord(
        path=str(path),
        repo=repo,
        sha1=sha1,
        language="java",
        package=pkg,
        loc=loc,
    )
    result = FileParseResult(file=file_rec, imports=imports)

    big_file = loc > LARGE_FILE_LINE_THRESHOLD

    type_decl_kinds = (
        ("class_declaration", "class"),
        ("interface_declaration", "interface"),
        ("enum_declaration", "enum"),
        ("record_declaration", "record"),
    )

    for kind_node, kind_label in type_decl_kinds:
        for decl in _find_all(root, kind_node):
            ident = _find_child(decl, "identifier")
            if ident is None:
                continue
            simple = _node_text(src, ident)
            fqn = f"{pkg}.{simple}" if pkg else simple
            cls_sym = SymbolRecord(
                fqn=fqn,
                simple=simple,
                kind=kind_label,
                file_path=str(path),
                repo=repo,
                start_line=decl.start_point[0] + 1,
                end_line=decl.end_point[0] + 1,
                modifiers=_modifier_strings(src, decl),
            )
            result.symbols.append(cls_sym)

            sup = _find_child(decl, "superclass")
            if sup is not None:
                for tc in sup.children:
                    if tc.type in ("type_identifier", "generic_type",
                                   "scoped_type_identifier"):
                        super_simple = _node_text(src, tc).split("<", 1)[0].strip()
                        if super_simple:
                            result.extends_edges.append((fqn, super_simple))

            sup_iface = _find_child(decl, "super_interfaces")
            if sup_iface is not None:
                for sub in _find_all(sup_iface, "type_identifier"):
                    iface_simple = _node_text(src, sub).strip()
                    if iface_simple:
                        result.implements_edges.append((fqn, iface_simple))

            body = (
                _find_child(decl, "class_body")
                or _find_child(decl, "interface_body")
                or _find_child(decl, "enum_body")
            )
            if body is None:
                continue

            for member in body.children:
                if member.type in ("method_declaration", "constructor_declaration"):
                    mident = _find_child(member, "identifier")
                    if mident is None:
                        continue
                    mname = _node_text(src, mident)
                    mfqn = f"{fqn}.{mname}"
                    formal = _find_child(member, "formal_parameters")
                    param_types: list[str] = []
                    if formal is not None:
                        for p in _find_all(formal, "formal_parameter"):
                            t = (_find_child(p, "type_identifier")
                                 or _find_child(p, "generic_type")
                                 or _find_child(p, "scoped_type_identifier")
                                 or _find_child(p, "array_type"))
                            if t is not None:
                                param_types.append(
                                    _node_text(src, t).split("<", 1)[0].strip()
                                )
                    method_sym = SymbolRecord(
                        fqn=mfqn,
                        simple=mname,
                        kind="method",
                        file_path=str(path),
                        repo=repo,
                        start_line=member.start_point[0] + 1,
                        end_line=member.end_point[0] + 1,
                        return_type=_extract_type(src, member),
                        param_types=param_types,
                        modifiers=_modifier_strings(src, member),
                    )
                    result.symbols.append(method_sym)

                    if big_file:
                        continue
                    mbody = _find_child(member, "block")
                    if mbody is None:
                        continue
                    for inv in _find_all(mbody, "method_invocation"):
                        kids = [c for c in inv.children if c.type != "."]
                        idents = [
                            c for c in kids
                            if c.type in ("identifier", "field_access",
                                          "scoped_identifier")
                        ]
                        if not idents:
                            continue
                        callee_simple = _node_text(src, idents[-1]).split(".")[-1]
                        if callee_simple and callee_simple.isidentifier():
                            result.call_simples.append((mfqn, callee_simple))
                elif member.type == "field_declaration":
                    var_decl = _find_child(member, "variable_declarator")
                    if var_decl is None:
                        continue
                    name_node = _find_child(var_decl, "identifier")
                    if name_node is None:
                        continue
                    fname = _node_text(src, name_node)
                    ffqn = f"{fqn}.{fname}"
                    field_sym = SymbolRecord(
                        fqn=ffqn,
                        simple=fname,
                        kind="field",
                        file_path=str(path),
                        repo=repo,
                        start_line=member.start_point[0] + 1,
                        end_line=member.end_point[0] + 1,
                        return_type=_extract_type(src, member),
                        modifiers=_modifier_strings(src, member),
                    )
                    result.symbols.append(field_sym)
    return result
