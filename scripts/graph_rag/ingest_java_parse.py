"""Graph-RAG ingester v2 — tree-sitter parsing (split from ingest_java.py).

Tree-sitter helpers, body-scanning heuristics, and the file/repo walkers that
produce ClassInfo/MethodInfo. Byte-identical move — no behaviour change.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

from ingest_java_model import LAYER_RULES, ClassInfo, MethodInfo

JAVA = Language(tsjava.language())
PARSER = Parser(JAVA)


# ─────────────── tree-sitter helpers ───────────────

def node_text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def find_all(node, kind: str):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            yield n
        stack.extend(reversed(n.children))


def find_child(node, kind: str):
    for c in node.children:
        if c.type == kind:
            return c
    return None


def ann_name(src: bytes, ann) -> str:
    for c in ann.children:
        if c.type in ("identifier", "scoped_identifier"):
            return node_text(src, c)
    return node_text(src, ann).lstrip("@").split("(")[0]


_STRLIT_RX = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')


def ann_strings(src: bytes, ann) -> list[str]:
    return _STRLIT_RX.findall(node_text(src, ann))


HTTP_ANNOTATIONS = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
    "RequestMapping": "ANY",
}


def collect_javadoc(src: bytes, target_node) -> str:
    """Return the block_comment immediately preceding target_node, if any."""
    prev = target_node.prev_sibling
    while prev is not None and prev.type in ("line_comment",):
        prev = prev.prev_sibling
    if prev is not None and prev.type == "block_comment":
        return node_text(src, prev).strip()[:1200]
    return ""


# ─────────────── body scanning heuristics ───────────────

_MONGO_COLL_ANN_RX = re.compile(r'@Document\s*\(\s*(?:collection\s*=\s*)?"([^"]+)"')
# Spring Data Mongo Template / Repository hints.
_MONGO_TEMPLATE_CALL_RX = re.compile(
    r'mongoTemplate\.(find\w*|count\w*|exists\w*|insert\w*|save\w*|update\w*|'
    r'remove\w*|delete\w*|bulkOps\w*)\b'
)
_MONGO_REPO_METHOD_RX = re.compile(
    r'\b(find\w+|count\w*|exists\w*|save\w*|insert\w*|update\w*|delete\w*)\('
)
# Explicit collection hint: query/document Class type → go via field type.
_URL_LITERAL_RX = re.compile(r'"(https?://[^"]+|/v1/[^"]+|/api/[^"]+)"')
_WEBCLIENT_URI_RX = re.compile(r'\.uri\s*\(\s*"([^"]+)"')
_RESTTEMPLATE_RX = re.compile(r'restTemplate\.(get|post|put|delete|exchange)For\w*')
_NATS_PUB_RX = re.compile(r'(?:publisher|natsClient|connection)\.publish\s*\(\s*"([^"]+)"')
_NATS_SUB_RX = re.compile(r'(?:subscribe|subscribeSync|subscribeAsync)\s*\(\s*"([^"]+)"')


def classify_layer(annotations: list[str], fqn: str) -> str:
    an_set = set(annotations)
    for mark, layer in LAYER_RULES:
        if mark.lstrip("@") in an_set:
            return layer
    lower = fqn.lower()
    if lower.endswith("controller"):
        return "controller"
    if lower.endswith("service") or lower.endswith("serviceimpl"):
        return "service"
    if lower.endswith("repository") or lower.endswith("dao"):
        return "repository"
    if "saga" in lower or "workflow" in lower:
        return "workflow"
    if lower.endswith("dto") or "/model/" in fqn or "/dao/" in fqn:
        return "model"
    if lower.endswith("mapper"):
        return "mapper"
    if lower.endswith("config") or lower.endswith("configuration"):
        return "config"
    return "other"


# ─────────────── file parse ───────────────

def parse_file(path: Path, src: bytes, collection_hints: dict[str, str]
               ) -> tuple[list[ClassInfo], list[MethodInfo]]:
    tree = PARSER.parse(src)
    root = tree.root_node

    pkg = ""
    imports: list[str] = []
    for n in root.children:
        if n.type == "package_declaration":
            pkg = node_text(src, n).replace("package ", "").rstrip(";").strip()
        elif n.type == "import_declaration":
            imp = node_text(src, n).replace("import ", "").rstrip(";").strip()
            if imp and imp != "static":
                imports.append(imp.replace("static ", ""))

    classes: list[ClassInfo] = []
    methods: list[MethodInfo] = []

    for decl in find_all(root, "class_declaration"):
        c = build_class(src, decl, pkg, path, "class", imports)
        classes.append(c)
        for m in extract_methods(src, decl, c, collection_hints):
            methods.append(m)
    for decl in find_all(root, "interface_declaration"):
        c = build_class(src, decl, pkg, path, "interface", imports)
        classes.append(c)
        for m in extract_methods(src, decl, c, collection_hints):
            methods.append(m)
    for decl in find_all(root, "enum_declaration"):
        c = build_class(src, decl, pkg, path, "enum", imports)
        classes.append(c)
    for decl in find_all(root, "record_declaration"):
        c = build_class(src, decl, pkg, path, "record", imports)
        classes.append(c)
    return classes, methods


def build_class(src: bytes, decl, pkg: str, path: Path, kind: str,
                imports: list[str]) -> ClassInfo:
    simple = "?"
    ident = find_child(decl, "identifier")
    if ident is not None:
        simple = node_text(src, ident)
    fqn = f"{pkg}.{simple}" if pkg else simple
    c = ClassInfo(fqn=fqn, simple=simple, kind=kind, file=str(path),
                  package=pkg, imports=imports)
    c.loc = decl.end_point[0] - decl.start_point[0] + 1

    c.javadoc = collect_javadoc(src, decl)

    mods = find_child(decl, "modifiers")
    if mods is not None:
        for a in mods.children:
            if a.type in ("annotation", "marker_annotation"):
                name = ann_name(src, a)
                c.annotations.append(name)
                if name == "RequestMapping":
                    ss = ann_strings(src, a)
                    if ss:
                        c.class_path_prefix = ss[0]
                elif name == "Transactional":
                    c.transactional = True
                elif name == "Async":
                    c.async_ = True
                elif name == "Scheduled":
                    c.scheduled = True
                elif name == "Cacheable":
                    c.cacheable = True

    c.layer = classify_layer(c.annotations, c.fqn)

    sup = find_child(decl, "superclass")
    if sup is not None:
        for tc in sup.children:
            if tc.type in ("type_identifier", "generic_type", "scoped_type_identifier"):
                c.extends.append(node_text(src, tc).split("<")[0])
    sup_iface = find_child(decl, "super_interfaces")
    if sup_iface is not None:
        for sub in find_all(sup_iface, "type_identifier"):
            c.implements.append(node_text(src, sub))

    body = find_child(decl, "class_body") or find_child(decl, "interface_body")
    if body is not None:
        for fd in find_all(body, "field_declaration"):
            type_node = find_child(fd, "type_identifier") or find_child(fd, "generic_type")
            var_decl = find_child(fd, "variable_declarator")
            if type_node is not None and var_decl is not None:
                type_name = node_text(src, type_node).split("<")[0]
                name_node = find_child(var_decl, "identifier")
                if name_node is not None:
                    c.autowired_fields.append(
                        (node_text(src, name_node), type_name)
                    )
    return c


def extract_methods(src: bytes, decl, cinfo: ClassInfo,
                    collection_hints: dict[str, str]) -> list[MethodInfo]:
    out: list[MethodInfo] = []
    body = find_child(decl, "class_body") or find_child(decl, "interface_body")
    if body is None:
        return out

    # pre-compute name→type map for autowired fields
    field_types = {name: t for name, t in cinfo.autowired_fields}

    for m in body.children:
        if m.type not in ("method_declaration", "constructor_declaration"):
            continue
        ident = find_child(m, "identifier")
        name = node_text(src, ident) if ident is not None else "?"
        sig_node = find_child(m, "formal_parameters")
        sig = node_text(src, sig_node) if sig_node is not None else "()"
        line = m.start_point[0] + 1
        loc = m.end_point[0] - m.start_point[0] + 1
        mi = MethodInfo(class_fqn=cinfo.fqn, name=name, sig=sig,
                        file=cinfo.file, line=line, loc=loc)
        mi.javadoc = collect_javadoc(src, m)

        # return type (just before the method name in AST)
        rt = None
        for c in m.children:
            if c.type in ("type_identifier", "generic_type", "void_type",
                          "integral_type", "floating_point_type", "boolean_type",
                          "scoped_type_identifier", "array_type"):
                rt = node_text(src, c)
                break
        mi.return_type = (rt or "").split("\n")[0][:200]

        # annotations
        mods = find_child(m, "modifiers")
        if mods is not None:
            for a in mods.children:
                if a.type in ("annotation", "marker_annotation"):
                    an = ann_name(src, a)
                    mi.annotations.append(an)
                    if an in HTTP_ANNOTATIONS:
                        ss = ann_strings(src, a)
                        sub_path = ss[0] if ss else ""
                        full = (cinfo.class_path_prefix + sub_path).replace("//", "/")
                        mi.endpoint = (HTTP_ANNOTATIONS[an], full, sig)
                    elif an == "Transactional":
                        mi.transactional = True
                    elif an == "Async":
                        mi.async_ = True
                    elif an == "Scheduled":
                        mi.scheduled = True
                    elif an == "Cacheable":
                        mi.cacheable = True

        # param types
        if sig_node is not None:
            for p in find_all(sig_node, "formal_parameter"):
                t = find_child(p, "type_identifier") or find_child(p, "generic_type")
                if t is not None:
                    mi.param_types.append(node_text(src, t).split("<")[0])

        mbody = find_child(m, "block")
        if mbody is None:
            out.append(mi)
            continue

        body_text = node_text(src, mbody)
        mi.body_snippet = body_text[:2500]

        # local variable types
        for ld in find_all(mbody, "local_variable_declaration"):
            t = find_child(ld, "type_identifier") or find_child(ld, "generic_type")
            vd = find_child(ld, "variable_declarator")
            if t is not None and vd is not None:
                type_name = node_text(src, t).split("<")[0]
                nid = find_child(vd, "identifier")
                if nid is not None:
                    mi.local_vars[node_text(src, nid)] = type_name

        # method invocations — attempt receiver resolution
        for inv in find_all(mbody, "method_invocation"):
            # structure: [receiver].[identifier](args)  or [identifier](args)
            recv_name: str | None = None
            method_name: str | None = None
            kids = [c for c in inv.children if c.type != "."]
            idents = [c for c in kids if c.type in ("identifier", "field_access",
                                                   "scoped_identifier")]
            if len(idents) >= 2:
                recv_txt = node_text(src, idents[-2])
                recv_name = recv_txt.split(".")[-1]
                method_name = node_text(src, idents[-1])
            elif len(idents) == 1:
                method_name = node_text(src, idents[0])
            if method_name:
                mi.called_names.append((method_name, recv_name))

        # Mongo templates
        for mt in _MONGO_TEMPLATE_CALL_RX.finditer(body_text):
            op = mt.group(1)
            coll = _guess_collection_from_call(body_text, mt.start(),
                                               field_types, mi.local_vars,
                                               collection_hints)
            if op.startswith(("find", "count", "exists")):
                if coll: mi.mongo_reads.append(coll)
            elif op.startswith(("delete", "remove")):
                if coll: mi.mongo_deletes.append(coll)
            else:
                if coll: mi.mongo_writes.append(coll)

        # Spring Data repository calls on autowired repo fields
        for inv in find_all(mbody, "method_invocation"):
            recv = None
            call_ident = None
            idents = [c for c in inv.children if c.type in ("identifier",)]
            if len(idents) >= 2:
                recv, call_ident = idents[-2], idents[-1]
            else:
                continue
            recv_name = node_text(src, recv)
            recv_type = field_types.get(recv_name) or mi.local_vars.get(recv_name)
            if recv_type is None:
                continue
            if recv_type.endswith("Repository") or recv_type.endswith("Dao"):
                call_name = node_text(src, call_ident)
                hint_coll = collection_hints.get(recv_type)
                if _MONGO_REPO_METHOD_RX.match(call_name + "("):
                    if call_name.startswith(("find", "count", "exists", "read", "get")):
                        if hint_coll: mi.mongo_reads.append(hint_coll)
                    elif call_name.startswith(("delete", "remove")):
                        if hint_coll: mi.mongo_deletes.append(hint_coll)
                    else:
                        if hint_coll: mi.mongo_writes.append(hint_coll)

        # REST client hits → cross-service external endpoints
        for m_url in _WEBCLIENT_URI_RX.finditer(body_text):
            mi.external_urls.append(m_url.group(1)[:300])
        for m_url in _RESTTEMPLATE_RX.finditer(body_text):
            mi.external_urls.append(f"(restTemplate.{m_url.group(1)})")

        # NATS publish/subscribe
        for m_s in _NATS_PUB_RX.finditer(body_text):
            mi.nats_subjects.append((m_s.group(1), "publish"))
        for m_s in _NATS_SUB_RX.finditer(body_text):
            mi.nats_subjects.append((m_s.group(1), "subscribe"))

        out.append(mi)
    return out


def _guess_collection_from_call(body_text: str, pos: int,
                                field_types: dict[str, str],
                                local_vars: dict[str, str],
                                collection_hints: dict[str, str]) -> str | None:
    """Look at the next 400 chars after `mongoTemplate.X(` for `ClassName.class`
    and resolve via `collection_hints`. Cheap heuristic."""
    window = body_text[pos:pos + 400]
    m = re.search(r"(\w+)\.class", window)
    if m:
        cls = m.group(1)
        return collection_hints.get(cls) or cls
    return None


# ─────────────── pass 1: collect @Document collection name per class ───────

def index_collection_hints(repo: Path) -> dict[str, str]:
    """Two-pass prep: class simple name → mongo collection name (from @Document)."""
    out: dict[str, str] = {}
    for java in repo.rglob("*.java"):
        rel = java.relative_to(repo)
        if any(p in ("target", "build", ".idea") for p in rel.parts):
            continue
        try:
            txt = java.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in txt.splitlines():
            m = _MONGO_COLL_ANN_RX.search(line)
            if m:
                coll = m.group(1)
                # find a class name on a nearby line for association
                # simpler: next non-empty "class X" after that line
                idx = txt.find(line) + len(line)
                ahead = txt[idx: idx + 500]
                cm = re.search(r"(?:public\s+)?(?:class|record|enum)\s+(\w+)", ahead)
                if cm:
                    out[cm.group(1)] = coll
    return out


# ─────────────── walker ───────────────

def walk_repo(repo: Path, collection_hints: dict[str, str]):
    for java in repo.rglob("*.java"):
        rel = java.relative_to(repo)
        if any(p in ("target", "build", ".idea") for p in rel.parts):
            continue
        src = java.read_bytes()
        try:
            classes, methods = parse_file(java, src, collection_hints)
        except Exception as exc:
            print(f"parse fail {rel}: {exc}", file=sys.stderr)
            continue
        yield rel, classes, methods
