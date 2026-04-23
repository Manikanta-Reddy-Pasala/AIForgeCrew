#!/usr/bin/env python3
"""Graph-RAG ingester v2 — richer Java-aware graph.

Targets "claude-code" style context retrieval: import-aware call resolution,
method bodies + javadoc, layer tagging, Mongo R/W edges, REST-client
cross-service edges, Spring annotation flags. One-shot batched Neo4j writes.

Schema:
  (:Package {name})
  (:Class  {fqn, simple, kind, file, package, layer, loc, annotations,
            imports, javadoc, transactional, async, scheduled, cacheable})
  (:Method {fqn, name, sig, file, line, loc, return_type, body_snippet,
            javadoc, annotations, transactional, async, scheduled, cacheable})
  (:Endpoint {http, path, method_fqn, params})
  (:MongoCollection {name})
  (:NatsSubject {subject})
  (:ExternalEndpoint {url})     # cross-service REST target

  (Package)-[:CONTAINS_CLASS]->(Class)
  (Class)-[:CONTAINS]->(Method)
  (Class)-[:EXTENDS]->(Class)
  (Class)-[:IMPLEMENTS]->(Class)
  (Class)-[:USES {field}]->(Class)
  (Class)-[:IMPORTS]->(Class)
  (Class)-[:BINDS]->(MongoCollection)
  (Class)-[:PUBLISHES]->(NatsSubject)

  (Method)-[:EXPOSES]->(Endpoint)
  (Method)-[:CALLS {via, certainty}]->(Method)
  (Method)-[:READS|WRITES|DELETES]->(MongoCollection)
  (Method)-[:CALLS_EXTERNAL]->(ExternalEndpoint)

Usage:
    python ingest_java.py --repo <path> --reset
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_java as tsjava
from neo4j import GraphDatabase
from tree_sitter import Language, Parser

JAVA = Language(tsjava.language())
PARSER = Parser(JAVA)


# ─────────────── model ───────────────

LAYER_RULES = [
    ("@RestController", "controller"),
    ("@Controller", "controller"),
    ("@Service", "service"),
    ("@Repository", "repository"),
    ("@Configuration", "config"),
    ("@Component", "component"),
]


@dataclass
class ClassInfo:
    fqn: str
    simple: str
    kind: str
    file: str
    package: str
    layer: str = "other"
    loc: int = 0
    extends: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    class_path_prefix: str = ""
    autowired_fields: list[tuple[str, str]] = field(default_factory=list)
    javadoc: str = ""
    transactional: bool = False
    async_: bool = False
    scheduled: bool = False
    cacheable: bool = False


@dataclass
class MethodInfo:
    class_fqn: str
    name: str
    sig: str
    file: str
    line: int
    loc: int = 0
    return_type: str = ""
    annotations: list[str] = field(default_factory=list)
    endpoint: tuple[str, str, str] | None = None  # (HTTP, path, params)
    called_names: list[tuple[str, str | None]] = field(default_factory=list)  # (name, receiver)
    param_types: list[str] = field(default_factory=list)
    body_snippet: str = ""
    javadoc: str = ""
    mongo_reads: list[str] = field(default_factory=list)
    mongo_writes: list[str] = field(default_factory=list)
    mongo_deletes: list[str] = field(default_factory=list)
    external_urls: list[str] = field(default_factory=list)
    nats_subjects: list[tuple[str, str]] = field(default_factory=list)  # (subject, op)
    transactional: bool = False
    async_: bool = False
    scheduled: bool = False
    cacheable: bool = False
    local_vars: dict[str, str] = field(default_factory=dict)  # var_name -> Type


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


# ─────────────── writer ───────────────

SCHEMA = [
    "CREATE CONSTRAINT class_fqn IF NOT EXISTS FOR (c:Class) REQUIRE c.fqn IS UNIQUE",
    "CREATE CONSTRAINT method_fqn IF NOT EXISTS FOR (m:Method) REQUIRE m.fqn IS UNIQUE",
    "CREATE CONSTRAINT endpoint_path IF NOT EXISTS FOR (e:Endpoint) REQUIRE (e.http, e.path) IS UNIQUE",
    "CREATE CONSTRAINT coll_name IF NOT EXISTS FOR (c:MongoCollection) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT subj_name IF NOT EXISTS FOR (s:NatsSubject) REQUIRE s.subject IS UNIQUE",
    "CREATE CONSTRAINT pkg_name IF NOT EXISTS FOR (p:Package) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT ext_url IF NOT EXISTS FOR (x:ExternalEndpoint) REQUIRE x.url IS UNIQUE",
    "CREATE INDEX class_simple IF NOT EXISTS FOR (c:Class) ON (c.simple)",
    "CREATE INDEX method_name IF NOT EXISTS FOR (m:Method) ON (m.name)",
    "CREATE INDEX endpoint_path_idx IF NOT EXISTS FOR (e:Endpoint) ON (e.path)",
    "CREATE INDEX class_layer IF NOT EXISTS FOR (c:Class) ON (c.layer)",
]


def write_graph(driver, classes: list[ClassInfo], methods: list[MethodInfo]) -> None:
    by_simple: dict[str, list[str]] = defaultdict(list)
    for c in classes:
        by_simple[c.simple].append(c.fqn)

    with driver.session() as s:
        for q in SCHEMA:
            s.run(q)

        # Packages
        pkgs = sorted({c.package for c in classes if c.package})
        if pkgs:
            s.run(
                "UNWIND $rows AS r MERGE (p:Package {name: r}) RETURN count(*)",
                rows=pkgs,
            )

        # Classes
        s.run(
            "UNWIND $rows AS r "
            "MERGE (c:Class {fqn: r.fqn}) "
            "SET c.simple=r.simple, c.kind=r.kind, c.file=r.file, "
            "    c.package=r.package, c.layer=r.layer, c.loc=r.loc, "
            "    c.annotations=r.annotations, c.imports=r.imports, "
            "    c.javadoc=r.javadoc, "
            "    c.transactional=r.transactional, c.async=r.async_, "
            "    c.scheduled=r.scheduled, c.cacheable=r.cacheable "
            "WITH c, r WHERE r.package <> '' "
            "MATCH (p:Package {name: r.package}) MERGE (p)-[:CONTAINS_CLASS]->(c)",
            rows=[{
                "fqn": c.fqn, "simple": c.simple, "kind": c.kind,
                "file": c.file, "package": c.package, "layer": c.layer,
                "loc": c.loc, "annotations": c.annotations,
                "imports": c.imports, "javadoc": c.javadoc,
                "transactional": c.transactional, "async_": c.async_,
                "scheduled": c.scheduled, "cacheable": c.cacheable,
            } for c in classes],
        )

        # EXTENDS / IMPLEMENTS / USES / IMPORTS
        ext = []
        impl = []
        uses = []
        imp_rows = []
        for c in classes:
            for e in c.extends:
                targets = by_simple.get(e) or [e]
                ext.append({"a": c.fqn, "b": targets[0]})
            for i in c.implements:
                targets = by_simple.get(i) or [i]
                impl.append({"a": c.fqn, "b": targets[0]})
            for fname, ftype in c.autowired_fields:
                targets = by_simple.get(ftype) or [ftype]
                uses.append({"a": c.fqn, "b": targets[0], "f": fname})
            for imp in c.imports:
                simple = imp.split(".")[-1]
                if simple and simple in by_simple:
                    imp_rows.append({"a": c.fqn, "b": by_simple[simple][0]})
        if ext:
            s.run(
                "UNWIND $rows AS r MERGE (a:Class {fqn:r.a}) MERGE (b:Class {fqn:r.b}) "
                "MERGE (a)-[:EXTENDS]->(b)", rows=ext)
        if impl:
            s.run(
                "UNWIND $rows AS r MERGE (a:Class {fqn:r.a}) MERGE (b:Class {fqn:r.b}) "
                "MERGE (a)-[:IMPLEMENTS]->(b)", rows=impl)
        if uses:
            s.run(
                "UNWIND $rows AS r MERGE (a:Class {fqn:r.a}) MERGE (b:Class {fqn:r.b}) "
                "MERGE (a)-[u:USES {field:r.f}]->(b)", rows=uses)
        if imp_rows:
            s.run(
                "UNWIND $rows AS r MATCH (a:Class {fqn:r.a}), (b:Class {fqn:r.b}) "
                "MERGE (a)-[:IMPORTS]->(b)", rows=imp_rows)

        # Methods
        s.run(
            "UNWIND $rows AS r "
            "MERGE (m:Method {fqn: r.fqn}) "
            "SET m.name=r.name, m.sig=r.sig, m.file=r.file, m.line=r.line, "
            "    m.loc=r.loc, m.return_type=r.return_type, "
            "    m.body_snippet=r.body_snippet, m.javadoc=r.javadoc, "
            "    m.annotations=r.annotations, "
            "    m.transactional=r.transactional, m.async=r.async_, "
            "    m.scheduled=r.scheduled, m.cacheable=r.cacheable "
            "WITH m, r MATCH (c:Class {fqn: r.class}) MERGE (c)-[:CONTAINS]->(m)",
            rows=[{
                "fqn": f"{m.class_fqn}#{m.name}:{m.line}",
                "class": m.class_fqn, "name": m.name, "sig": m.sig,
                "file": m.file, "line": m.line, "loc": m.loc,
                "return_type": m.return_type,
                "body_snippet": m.body_snippet, "javadoc": m.javadoc,
                "annotations": m.annotations,
                "transactional": m.transactional, "async_": m.async_,
                "scheduled": m.scheduled, "cacheable": m.cacheable,
            } for m in methods],
        )

        # Endpoints
        ep_rows = []
        for m in methods:
            if m.endpoint:
                http, path, params = m.endpoint
                ep_rows.append({
                    "m_fqn": f"{m.class_fqn}#{m.name}:{m.line}",
                    "http": http, "path": path, "params": params[:400],
                })
        if ep_rows:
            s.run(
                "UNWIND $rows AS r "
                "MERGE (e:Endpoint {http: r.http, path: r.path}) "
                "SET e.params=r.params, e.method_fqn=r.m_fqn "
                "WITH e, r MATCH (m:Method {fqn: r.m_fqn}) "
                "MERGE (m)-[:EXPOSES]->(e)",
                rows=ep_rows,
            )

        # Method call resolution (import + field-type aware)
        # Build name -> [method_fqn] index.
        name_to_methods: dict[str, list[str]] = defaultdict(list)
        for m in methods:
            name_to_methods[m.name].append(f"{m.class_fqn}#{m.name}:{m.line}")

        # Field types map per class
        field_types_per_class: dict[str, dict[str, str]] = {}
        for c in classes:
            field_types_per_class[c.fqn] = {n: t for n, t in c.autowired_fields}

        call_rows = []
        for m in methods:
            src_fqn = f"{m.class_fqn}#{m.name}:{m.line}"
            ftypes = field_types_per_class.get(m.class_fqn, {})
            for called_name, recv in m.called_names:
                candidates = name_to_methods.get(called_name, [])
                if not candidates:
                    continue
                # Narrow by receiver type → containing class FQN match.
                if recv:
                    recv_type = ftypes.get(recv)
                    if recv_type:
                        target_fqns = by_simple.get(recv_type)
                        if target_fqns:
                            narrowed = [c for c in candidates
                                        if any(c.startswith(f + "#") for f in target_fqns)]
                            if narrowed:
                                call_rows.append({"a": src_fqn, "b": narrowed[0],
                                                   "via": called_name,
                                                   "cert": "resolved"})
                                continue
                # Fallback: first few candidates, marked unresolved.
                for b in candidates[:2]:
                    if b == src_fqn:
                        continue
                    call_rows.append({"a": src_fqn, "b": b,
                                      "via": called_name,
                                      "cert": "name_only"})
        # Batch writes in chunks of 1000
        for i in range(0, len(call_rows), 1000):
            chunk = call_rows[i:i + 1000]
            s.run(
                "UNWIND $rows AS r "
                "MATCH (a:Method {fqn: r.a}), (b:Method {fqn: r.b}) "
                "MERGE (a)-[c:CALLS {via: r.via}]->(b) "
                "SET c.certainty = r.cert",
                rows=chunk,
            )

        # Mongo R/W/D
        mongo_rows = []
        for m in methods:
            m_fqn = f"{m.class_fqn}#{m.name}:{m.line}"
            for coll in set(m.mongo_reads):
                mongo_rows.append({"m": m_fqn, "c": coll, "op": "READS"})
            for coll in set(m.mongo_writes):
                mongo_rows.append({"m": m_fqn, "c": coll, "op": "WRITES"})
            for coll in set(m.mongo_deletes):
                mongo_rows.append({"m": m_fqn, "c": coll, "op": "DELETES"})
        if mongo_rows:
            for op in ("READS", "WRITES", "DELETES"):
                sub = [r for r in mongo_rows if r["op"] == op]
                if sub:
                    s.run(
                        "UNWIND $rows AS r "
                        "MERGE (coll:MongoCollection {name: r.c}) "
                        "WITH coll, r MATCH (m:Method {fqn: r.m}) "
                        f"MERGE (m)-[:{op}]->(coll)",
                        rows=sub,
                    )

        # External endpoints
        ext_rows = []
        for m in methods:
            for url in set(m.external_urls):
                ext_rows.append({
                    "m": f"{m.class_fqn}#{m.name}:{m.line}", "u": url,
                })
        if ext_rows:
            s.run(
                "UNWIND $rows AS r "
                "MERGE (e:ExternalEndpoint {url: r.u}) "
                "WITH e, r MATCH (m:Method {fqn: r.m}) "
                "MERGE (m)-[:CALLS_EXTERNAL]->(e)",
                rows=ext_rows,
            )

        # NATS
        nats_rows = []
        for m in methods:
            for subj, op in m.nats_subjects:
                nats_rows.append({
                    "m": f"{m.class_fqn}#{m.name}:{m.line}",
                    "s": subj, "op": op.upper(),
                })
        if nats_rows:
            for op in ("PUBLISH", "SUBSCRIBE"):
                sub = [r for r in nats_rows if r["op"] == op]
                if sub:
                    s.run(
                        "UNWIND $rows AS r "
                        "MERGE (s:NatsSubject {subject: r.s}) "
                        "WITH s, r MATCH (m:Method {fqn: r.m}) "
                        f"MERGE (m)-[:{op}]->(s)",
                        rows=sub,
                    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--neo4j", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"repo not found: {repo}", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))

    if args.reset:
        with driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")
            print("reset")

    print("pass 1: indexing @Document collection hints ...")
    collection_hints = index_collection_hints(repo)
    print(f"  {len(collection_hints)} class→collection mappings")

    all_classes: list[ClassInfo] = []
    all_methods: list[MethodInfo] = []
    count = 0
    for rel, classes, methods in walk_repo(repo, collection_hints):
        all_classes.extend(classes)
        all_methods.extend(methods)
        count += 1
        if count % 100 == 0:
            print(f"... parsed {count} files")
    print(f"parsed {count} files | {len(all_classes)} classes | "
          f"{len(all_methods)} methods")

    write_graph(driver, all_classes, all_methods)

    with driver.session() as s:
        print("\n--- node counts ---")
        for l, c in s.run(
            "MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c ORDER BY c DESC"
        ).values():
            print(f"  {l}: {c}")
        print("\n--- relationship counts ---")
        for typ, c in s.run(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC"
        ).values():
            print(f"  {typ}: {c}")
    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
