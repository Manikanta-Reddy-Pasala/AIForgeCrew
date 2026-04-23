#!/usr/bin/env python3
"""Graph-RAG ingester — parse a Java repo and push a logic-aware graph to Neo4j.

Schema:
  (:Class {fqn, simple, kind, file})
  (:Method {fqn, name, sig, file, line})
  (:Endpoint {http, path})
  (:MongoCollection {name})
  (:NatsSubject {subject})
  (:Service {name, kind})  # kind=internal|external

  (Class)-[:CONTAINS]->(Method)
  (Class)-[:EXTENDS]->(Class)
  (Class)-[:IMPLEMENTS]->(Class)
  (Class)-[:ANNOTATED_WITH {name}]->()  # loose anchor for annotations
  (Method)-[:EXPOSES]->(Endpoint)
  (Method)-[:CALLS {via}]->(Method)
  (Method)-[:USES {how}]->(Class)       # autowired + parameter types
  (Method)-[:READS|WRITES]->(MongoCollection)
  (Method)-[:PUBLISHES|SUBSCRIBES]->(NatsSubject)
  (Class)-[:CALLS_SERVICE]->(Service)

Usage:
    python ingest_java.py --repo ~/Documents/codeRepo/PosClientBackend \\
        --package-prefix com.pos.backend --neo4j bolt://localhost:7687
"""
from __future__ import annotations

import argparse
import os
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


# ─────────────── data model ───────────────

@dataclass
class ClassInfo:
    fqn: str
    simple: str
    kind: str  # class|interface|enum|record
    file: str
    package: str
    extends: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    class_path_prefix: str = ""          # @RequestMapping on class level
    autowired_fields: list[tuple[str, str]] = field(default_factory=list)  # (name, type)


@dataclass
class MethodInfo:
    class_fqn: str
    name: str
    sig: str
    file: str
    line: int
    annotations: list[str] = field(default_factory=list)
    endpoint: tuple[str, str] | None = None  # (HTTP, path)
    called_names: list[str] = field(default_factory=list)
    param_types: list[str] = field(default_factory=list)


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


def ann_name(src: bytes, ann_node) -> str:
    """Return the annotation's simple name, e.g. @RequestMapping → RequestMapping."""
    for c in ann_node.children:
        if c.type in ("identifier", "scoped_identifier"):
            return node_text(src, c)
    return node_text(src, ann_node).lstrip("@").split("(")[0]


_STRLIT_RX = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')


def ann_string_args(src: bytes, ann_node) -> list[str]:
    """Pull any quoted string literals from an annotation call."""
    txt = node_text(src, ann_node)
    return _STRLIT_RX.findall(txt)


HTTP_ANNOTATIONS = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
    "RequestMapping": "ANY",
}


# ─────────────── file walker ───────────────

def parse_file(path: Path, src: bytes) -> tuple[list[ClassInfo], list[MethodInfo]]:
    tree = PARSER.parse(src)
    root = tree.root_node

    pkg = ""
    for n in root.children:
        if n.type == "package_declaration":
            # grab everything after "package"
            pkg_txt = node_text(src, n).replace("package ", "").rstrip(";").strip()
            pkg = pkg_txt
            break

    classes: list[ClassInfo] = []
    methods: list[MethodInfo] = []

    for decl in find_all(root, "class_declaration"):
        c = build_class(src, decl, pkg, path, kind="class")
        classes.append(c)
        for m in extract_methods(src, decl, c):
            methods.append(m)
    for decl in find_all(root, "interface_declaration"):
        c = build_class(src, decl, pkg, path, kind="interface")
        classes.append(c)
        for m in extract_methods(src, decl, c):
            methods.append(m)

    return classes, methods


def build_class(src: bytes, decl, pkg: str, path: Path, kind: str) -> ClassInfo:
    simple = "?"
    ident = find_child(decl, "identifier")
    if ident is not None:
        simple = node_text(src, ident)
    fqn = f"{pkg}.{simple}" if pkg else simple

    c = ClassInfo(fqn=fqn, simple=simple, kind=kind, file=str(path), package=pkg)

    # Annotations on class
    mods = find_child(decl, "modifiers")
    if mods is not None:
        for a in mods.children:
            if a.type in ("annotation", "marker_annotation"):
                name = ann_name(src, a)
                c.annotations.append(name)
                if name == "RequestMapping":
                    strs = ann_string_args(src, a)
                    if strs:
                        c.class_path_prefix = strs[0]

    # extends / implements
    sup = find_child(decl, "superclass")
    if sup is not None:
        for tc in sup.children:
            if tc.type in ("type_identifier", "generic_type", "scoped_type_identifier"):
                c.extends.append(node_text(src, tc))
    sup_iface = find_child(decl, "super_interfaces")
    if sup_iface is not None:
        for sub in find_all(sup_iface, "type_identifier"):
            c.implements.append(node_text(src, sub))

    # autowired fields
    body = find_child(decl, "class_body") or find_child(decl, "interface_body")
    if body is not None:
        for fd in find_all(body, "field_declaration"):
            fd_text = node_text(src, fd)
            if "@Autowired" in fd_text or "final " in fd_text:
                # best-effort: last word before `;` is the name; type is the first type ident.
                type_node = find_child(fd, "type_identifier") or find_child(fd, "generic_type")
                var_decl = find_child(fd, "variable_declarator")
                if type_node is not None and var_decl is not None:
                    type_name = node_text(src, type_node)
                    name_node = find_child(var_decl, "identifier")
                    if name_node is not None:
                        c.autowired_fields.append(
                            (node_text(src, name_node), type_name)
                        )
    return c


def extract_methods(src: bytes, decl, cinfo: ClassInfo) -> list[MethodInfo]:
    methods: list[MethodInfo] = []
    body = find_child(decl, "class_body") or find_child(decl, "interface_body")
    if body is None:
        return methods
    for m in body.children:
        if m.type not in ("method_declaration", "constructor_declaration"):
            continue
        ident = find_child(m, "identifier")
        name = node_text(src, ident) if ident is not None else "?"
        sig_node = find_child(m, "formal_parameters")
        sig = node_text(src, sig_node) if sig_node is not None else "()"
        line = m.start_point[0] + 1
        mi = MethodInfo(class_fqn=cinfo.fqn, name=name, sig=sig,
                        file=cinfo.file, line=line)
        # Annotations
        mods = find_child(m, "modifiers")
        if mods is not None:
            for a in mods.children:
                if a.type in ("annotation", "marker_annotation"):
                    an = ann_name(src, a)
                    mi.annotations.append(an)
                    if an in HTTP_ANNOTATIONS:
                        strs = ann_string_args(src, a)
                        sub_path = strs[0] if strs else ""
                        full = (cinfo.class_path_prefix + sub_path).replace("//", "/")
                        mi.endpoint = (HTTP_ANNOTATIONS[an], full)

        # Param types
        if sig_node is not None:
            for p in find_all(sig_node, "formal_parameter"):
                t = find_child(p, "type_identifier") or find_child(p, "generic_type")
                if t is not None:
                    mi.param_types.append(node_text(src, t))

        # Called names (method_invocation)
        mbody = find_child(m, "block")
        if mbody is not None:
            for inv in find_all(mbody, "method_invocation"):
                ident_child = None
                for c in inv.children:
                    if c.type == "identifier":
                        ident_child = c
                        break
                if ident_child is not None:
                    mi.called_names.append(node_text(src, ident_child))

        methods.append(mi)
    return methods


# ─────────────── heuristic extras ───────────────

_MONGO_COLL_RX = re.compile(r'@Document\s*\(\s*(?:collection\s*=\s*)?"([^"]+)"')
_NATS_SUBJ_RX = re.compile(r'(?:subject|subjectName|SUBJECT|Subject)\s*=\s*"([^"]+)"')


def scan_heuristics(src_text: str) -> dict:
    out = {"mongo_collections": set(), "nats_subjects": set()}
    for m in _MONGO_COLL_RX.finditer(src_text):
        out["mongo_collections"].add(m.group(1))
    for m in _NATS_SUBJ_RX.finditer(src_text):
        out["nats_subjects"].add(m.group(1))
    return out


# ─────────────── walk repo ───────────────

def walk_repo(root: Path, prefix: str | None):
    for java in root.rglob("*.java"):
        rel = java.relative_to(root)
        if any(part in ("target", "build", ".idea") for part in rel.parts):
            continue
        src = java.read_bytes()
        try:
            classes, methods = parse_file(java, src)
        except Exception as exc:
            print(f"parse fail {rel}: {exc}", file=sys.stderr)
            continue
        text = src.decode("utf-8", errors="replace")
        heur = scan_heuristics(text)
        yield rel, classes, methods, heur


# ─────────────── Neo4j writer ───────────────

SCHEMA_CYPHER = [
    "CREATE CONSTRAINT class_fqn IF NOT EXISTS FOR (c:Class) REQUIRE c.fqn IS UNIQUE",
    "CREATE CONSTRAINT method_fqn IF NOT EXISTS FOR (m:Method) REQUIRE m.fqn IS UNIQUE",
    "CREATE CONSTRAINT endpoint_path IF NOT EXISTS FOR (e:Endpoint) REQUIRE (e.http, e.path) IS UNIQUE",
    "CREATE CONSTRAINT coll_name IF NOT EXISTS FOR (c:MongoCollection) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT subj_name IF NOT EXISTS FOR (s:NatsSubject) REQUIRE s.subject IS UNIQUE",
]


def write_graph(driver, classes, methods, heur_by_file):
    by_simple: dict[str, str] = {}   # simple -> fqn (first wins)
    for c in classes:
        by_simple.setdefault(c.simple, c.fqn)

    with driver.session() as s:
        for q in SCHEMA_CYPHER:
            s.run(q)

        # Classes
        s.run(
            "UNWIND $rows AS r "
            "MERGE (c:Class {fqn: r.fqn}) "
            "SET c.simple=r.simple, c.kind=r.kind, c.file=r.file, "
            "    c.package=r.package, c.annotations=r.annotations",
            rows=[{
                "fqn": c.fqn, "simple": c.simple, "kind": c.kind,
                "file": c.file, "package": c.package,
                "annotations": c.annotations,
            } for c in classes],
        )

        # EXTENDS / IMPLEMENTS
        for c in classes:
            for ext in c.extends:
                target = by_simple.get(ext.split("<")[0], ext)
                s.run(
                    "MERGE (a:Class {fqn: $a}) MERGE (b:Class {fqn: $b}) "
                    "MERGE (a)-[:EXTENDS]->(b)", a=c.fqn, b=target,
                )
            for imp in c.implements:
                target = by_simple.get(imp, imp)
                s.run(
                    "MERGE (a:Class {fqn: $a}) MERGE (b:Class {fqn: $b}) "
                    "MERGE (a)-[:IMPLEMENTS]->(b)", a=c.fqn, b=target,
                )
            for fname, ftype in c.autowired_fields:
                target = by_simple.get(ftype, ftype)
                s.run(
                    "MERGE (a:Class {fqn: $a}) MERGE (b:Class {fqn: $b}) "
                    "MERGE (a)-[:USES {field: $f}]->(b)",
                    a=c.fqn, b=target, f=fname,
                )

        # Methods
        s.run(
            "UNWIND $rows AS r "
            "MERGE (m:Method {fqn: r.fqn}) "
            "SET m.name=r.name, m.sig=r.sig, m.file=r.file, m.line=r.line, "
            "    m.annotations=r.annotations "
            "WITH m, r MATCH (c:Class {fqn: r.class}) MERGE (c)-[:CONTAINS]->(m)",
            rows=[{
                "fqn": f"{m.class_fqn}#{m.name}",
                "class": m.class_fqn, "name": m.name, "sig": m.sig,
                "file": m.file, "line": m.line, "annotations": m.annotations,
            } for m in methods],
        )

        # Endpoints
        ep_rows = []
        for m in methods:
            if m.endpoint:
                http, path = m.endpoint
                ep_rows.append({
                    "fqn": f"{m.class_fqn}#{m.name}",
                    "http": http, "path": path,
                })
        if ep_rows:
            s.run(
                "UNWIND $rows AS r "
                "MERGE (e:Endpoint {http: r.http, path: r.path}) "
                "WITH e, r MATCH (m:Method {fqn: r.fqn}) "
                "MERGE (m)-[:EXPOSES]->(e)",
                rows=ep_rows,
            )

        # Calls — name-only match (best effort)
        all_method_names = defaultdict(list)
        for m in methods:
            all_method_names[m.name].append(f"{m.class_fqn}#{m.name}")
        call_rows = []
        for m in methods:
            src_fqn = f"{m.class_fqn}#{m.name}"
            for called in set(m.called_names):
                targets = all_method_names.get(called, [])
                for t in targets[:5]:
                    if t == src_fqn:
                        continue
                    call_rows.append({"a": src_fqn, "b": t, "via": called})
        if call_rows:
            s.run(
                "UNWIND $rows AS r "
                "MATCH (a:Method {fqn: r.a}), (b:Method {fqn: r.b}) "
                "MERGE (a)-[c:CALLS {via: r.via}]->(b)",
                rows=call_rows,
            )

        # Mongo collections / Nats subjects
        for file_key, heur in heur_by_file.items():
            for coll in heur["mongo_collections"]:
                s.run(
                    "MERGE (c:MongoCollection {name: $n}) "
                    "WITH c MATCH (cls:Class {file: $f}) "
                    "MERGE (cls)-[:BINDS]->(c)",
                    n=coll, f=file_key,
                )
            for subj in heur["nats_subjects"]:
                s.run(
                    "MERGE (s:NatsSubject {subject: $s_}) "
                    "WITH s MATCH (cls:Class {file: $f}) "
                    "MERGE (cls)-[:PUBLISHES]->(s)",
                    s_=subj, f=file_key,
                )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--neo4j", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--reset", action="store_true",
                    help="DETACH DELETE all nodes before ingest")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"repo not found: {repo}", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))

    if args.reset:
        with driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")
            print("reset: all nodes deleted")

    all_classes: list[ClassInfo] = []
    all_methods: list[MethodInfo] = []
    heur_by_file: dict[str, dict] = {}
    file_count = 0
    for rel, classes, methods, heur in walk_repo(repo, None):
        all_classes.extend(classes)
        all_methods.extend(methods)
        if classes:
            heur_by_file[classes[0].file] = heur
        file_count += 1
        if file_count % 100 == 0:
            print(f"... parsed {file_count} files")

    print(f"parsed {file_count} files | "
          f"{len(all_classes)} classes | {len(all_methods)} methods")

    write_graph(driver, all_classes, all_methods, heur_by_file)

    with driver.session() as s:
        counts = s.run(
            "MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c ORDER BY c DESC"
        ).values()
        print("node counts:")
        for l, c in counts:
            print(f"  {l}: {c}")
    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
