#!/usr/bin/env python3
"""Ingest a JSONL file produced by the JavaParser Extractor into Neo4j.

Richer than ingest_java.py (tree-sitter): symbol-solved calls, param &
return type edges, throws edges, full body (4KB), exact fields (only
@Autowired/@Inject/@Value/@Resource or final), constructor nodes.

Added relationships beyond v2:
  (Method)-[:PARAM_TYPE {pos, name}]->(Class)
  (Method)-[:RETURNS]->(Class)
  (Method)-[:THROWS]->(Class)
  (Class)-[:ANNOTATED {name}]->(Annotation)
  (Method)-[:ANNOTATED {name}]->(Annotation)

Usage:
    python ingest_jsonl.py --jsonl /tmp/pcb-ast.jsonl \\
        --neo4j bolt://192.168.70.191:7687 --reset
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from neo4j import GraphDatabase


SCHEMA = [
    "CREATE CONSTRAINT class_fqn IF NOT EXISTS FOR (c:Class) REQUIRE c.fqn IS UNIQUE",
    "CREATE CONSTRAINT method_fqn IF NOT EXISTS FOR (m:Method) REQUIRE m.fqn IS UNIQUE",
    "CREATE CONSTRAINT endpoint IF NOT EXISTS FOR (e:Endpoint) REQUIRE (e.http, e.path) IS UNIQUE",
    "CREATE CONSTRAINT pkg IF NOT EXISTS FOR (p:Package) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT coll IF NOT EXISTS FOR (c:MongoCollection) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT subj IF NOT EXISTS FOR (s:NatsSubject) REQUIRE s.subject IS UNIQUE",
    "CREATE CONSTRAINT ann IF NOT EXISTS FOR (a:Annotation) REQUIRE a.name IS UNIQUE",
    "CREATE CONSTRAINT ext IF NOT EXISTS FOR (e:ExternalEndpoint) REQUIRE e.url IS UNIQUE",
    "CREATE INDEX class_simple IF NOT EXISTS FOR (c:Class) ON (c.simple)",
    "CREATE INDEX class_layer IF NOT EXISTS FOR (c:Class) ON (c.layer)",
    "CREATE INDEX method_name IF NOT EXISTS FOR (m:Method) ON (m.name)",
    "CREATE INDEX endpoint_path IF NOT EXISTS FOR (e:Endpoint) ON (e.path)",
]


_URL_RX = re.compile(r'"(https?://[^"]+|/v1/[^"]+|/api/[^"]+)"')

# Broadened: any <identifier>.publish("literal"). Catches jetStream, localJetStream,
# natsConnection, publisher, nc, etc. without an allowlist.
_NATS_PUB_RX = re.compile(
    r'\b[A-Za-z_][A-Za-z0-9_]*\.publish\s*\(\s*"([a-zA-Z][a-zA-Z0-9._\-\*]*)"'
)
# Broadened: any <identifier>.subscribe("literal") where the identifier suggests
# a JetStream / NATS context (filters out Reactor Mono.subscribe noise by requiring
# a string-literal first arg).
_NATS_SUB_RX = re.compile(
    r'\b[A-Za-z_][A-Za-z0-9_]*\.subscribe\s*\(\s*"([a-zA-Z][a-zA-Z0-9._\-\*]*)"'
)
# Constant-resolved subscribe: `x.subscribe(SUBJECT_PATTERN, ...)` where
# SUBJECT_PATTERN is a String constant in the same class. Captures the
# identifier so the second pass can swap it for the literal value.
_NATS_SUB_CONST_RX = re.compile(
    r'\b[A-Za-z_][A-Za-z0-9_]*\.subscribe\s*\(\s*([A-Z][A-Z0-9_]*)\b'
)
_STRING_CONST_RX = re.compile(
    r'\bstatic\s+final\s+String\s+([A-Z][A-Z0-9_]*)\s*=\s*"([^"]+)"'
)

# Kafka: kafkaTemplate.send("topic", ...)  OR wrapped sendWithCallback("topic", ...)
_KAFKA_PUB_RX = re.compile(
    r'\b(?:kafkaTemplate|kafkaProductTemplate|[a-zA-Z_][a-zA-Z0-9_]*Template)\.send'
    r'\s*\(\s*"([a-zA-Z][a-zA-Z0-9._\-]*)"'
)
_KAFKA_WRAP_RX = re.compile(
    r'\bsendWithCallback\s*\(\s*"([a-zA-Z][a-zA-Z0-9._\-]*)"'
)
# @KafkaListener(topics = "stock" ...)  OR  @KafkaListener(topics={"a","b"} ...)
_KAFKA_LISTENER_RX = re.compile(
    r'@KafkaListener\s*\([^)]*topics\s*=\s*(?:"([^"]+)"|\{([^}]+)\})',
    re.DOTALL,
)

_MONGO_TEMPLATE_RX = re.compile(
    r'mongoTemplate\.(find\w*|count\w*|exists\w*|insert\w*|save\w*|update\w*|'
    r'remove\w*|delete\w*|bulkOps\w*)'
)
_DOCUMENT_ANN_RX = re.compile(r'@Document\s*\(\s*(?:collection\s*=\s*)?"([^"]+)"')


def load_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def index_collection_hints(records: list[dict]) -> dict[str, str]:
    """Simple name → mongo collection from @Document (body text scan over file)."""
    out: dict[str, str] = {}
    for rec in records:
        for cls in rec.get("classes") or []:
            # body is per-method; annotations on class include @Document
            # but JavaParser gives annotation names only. Scan methods bodies
            # as fallback to catch the string.
            text_parts = [cls.get("javadoc") or ""]
            for m in cls.get("methods") or []:
                text_parts.append(m.get("body") or "")
            blob = "\n".join(text_parts)
            m = _DOCUMENT_ANN_RX.search(blob)
            if m:
                out[cls["simple"]] = m.group(1)
    return out


def collect_all(records: list[dict]):
    classes_by_simple: dict[str, list[str]] = defaultdict(list)
    all_classes: list[dict] = []
    all_methods: list[dict] = []
    for rec in records:
        imports = rec.get("imports") or []
        for cls in rec.get("classes") or []:
            cls["file"] = rec["file"]
            cls["imports"] = imports
            classes_by_simple[cls["simple"]].append(cls["fqn"])
            all_classes.append(cls)
            for m in cls.get("methods") or []:
                m["class_fqn"] = cls["fqn"]
                m["class_simple"] = cls["simple"]
                m["file"] = rec["file"]
                all_methods.append(m)
    return all_classes, all_methods, classes_by_simple


def batch(iterable, size: int):
    buf = []
    for x in iterable:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def resolve_class(fqn_hint: str, by_simple: dict[str, list[str]]) -> str:
    if not fqn_hint:
        return ""
    simple = fqn_hint.split(".")[-1]
    if simple in by_simple and by_simple[simple]:
        return by_simple[simple][0]
    return fqn_hint


def write_graph(driver, records: list[dict], collection_hints: dict[str, str]):
    classes, methods, by_simple = collect_all(records)
    classes_by_fqn = {c["fqn"]: c for c in classes}

    with driver.session() as s:
        for q in SCHEMA:
            s.run(q)

        # Packages
        pkgs = sorted({c.get("package", "") for c in classes if c.get("package")})
        if pkgs:
            s.run("UNWIND $rows AS n MERGE (:Package {name: n})", rows=pkgs)

        # Classes + CONTAINS_CLASS
        rows = [{
            "fqn": c["fqn"], "simple": c["simple"], "kind": c["kind"],
            "file": c["file"], "package": c.get("package", ""),
            "layer": c.get("layer", "other"),
            "start_line": c.get("start_line", 0),
            "loc": c.get("loc", 0),
            "annotations": c.get("annotations") or [],
            "imports": c.get("imports") or [],
            "javadoc": c.get("javadoc") or "",
            "transactional": bool(c.get("transactional")),
            "async_": bool(c.get("async")),
            "scheduled": bool(c.get("scheduled")),
            "cacheable": bool(c.get("cacheable")),
        } for c in classes]
        s.run(
            "UNWIND $rows AS r "
            "MERGE (c:Class {fqn: r.fqn}) "
            "SET c.simple=r.simple, c.kind=r.kind, c.file=r.file, "
            "    c.package=r.package, c.layer=r.layer, c.loc=r.loc, "
            "    c.start_line=r.start_line, c.annotations=r.annotations, "
            "    c.imports=r.imports, c.javadoc=r.javadoc, "
            "    c.transactional=r.transactional, c.async=r.async_, "
            "    c.scheduled=r.scheduled, c.cacheable=r.cacheable "
            "WITH c, r WHERE r.package <> '' "
            "MATCH (p:Package {name: r.package}) "
            "MERGE (p)-[:CONTAINS_CLASS]->(c)",
            rows=rows,
        )

        # Annotations as nodes
        ann_names = set()
        for c in classes:
            for a in c.get("annotations") or []:
                ann_names.add(a)
            for m in c.get("methods") or []:
                for a in m.get("annotations") or []:
                    ann_names.add(a)
        if ann_names:
            s.run("UNWIND $rows AS a MERGE (:Annotation {name: a})",
                  rows=sorted(ann_names))
            class_ann_rows = []
            method_ann_rows = []
            for c in classes:
                for a in c.get("annotations") or []:
                    class_ann_rows.append({"src": c["fqn"], "a": a})
                for m in c.get("methods") or []:
                    for a in m.get("annotations") or []:
                        method_ann_rows.append({
                            "src": m["fqn"], "a": a,
                        })
            if class_ann_rows:
                s.run(
                    "UNWIND $rows AS r "
                    "MATCH (c:Class {fqn: r.src}), (a:Annotation {name: r.a}) "
                    "MERGE (c)-[:ANNOTATED]->(a)",
                    rows=class_ann_rows,
                )
            if method_ann_rows:
                s.run(
                    "UNWIND $rows AS r "
                    "MATCH (m:Method {fqn: r.src}), (a:Annotation {name: r.a}) "
                    "MERGE (m)-[:ANNOTATED]->(a)",
                    rows=method_ann_rows,
                )

        # EXTENDS / IMPLEMENTS / USES (from fields) / IMPORTS
        ext = []
        impl = []
        uses = []
        imp_rows = []
        for c in classes:
            for e in c.get("extends") or []:
                ext.append({"a": c["fqn"], "b": resolve_class(e, by_simple)})
            for i in c.get("implements") or []:
                impl.append({"a": c["fqn"], "b": resolve_class(i, by_simple)})
            for fld in c.get("fields") or []:
                t = fld.get("type") or ""
                uses.append({
                    "a": c["fqn"], "b": resolve_class(t, by_simple),
                    "f": fld.get("name", ""),
                    "final": bool(fld.get("final")),
                    "inject": bool(fld.get("inject")),
                })
            for imp in c.get("imports") or []:
                simple = imp.split(".")[-1].split(" ")[0]
                if simple in by_simple and by_simple[simple]:
                    imp_rows.append({"a": c["fqn"], "b": by_simple[simple][0]})
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
                "MERGE (a)-[u:USES {field:r.f}]->(b) "
                "SET u.final=r.final, u.inject=r.inject", rows=uses)
        if imp_rows:
            s.run(
                "UNWIND $rows AS r MATCH (a:Class {fqn:r.a}), (b:Class {fqn:r.b}) "
                "MERGE (a)-[:IMPORTS]->(b)", rows=imp_rows)

        # Methods + CONTAINS + param / return / throws edges
        method_rows = [{
            "fqn": m["fqn"], "class_fqn": m["class_fqn"],
            "name": m["name"], "sig": m["sig"],
            "return_type": m.get("return_type", ""),
            "line": m.get("line", 0), "loc": m.get("loc", 0),
            "annotations": m.get("annotations") or [],
            "body": (m.get("body") or "")[:4000],
            "javadoc": m.get("javadoc") or "",
            "file": m["file"],
            "transactional": bool(m.get("transactional")),
            "async_": bool(m.get("async")),
            "scheduled": bool(m.get("scheduled")),
            "cacheable": bool(m.get("cacheable")),
        } for m in methods]
        for chunk in batch(method_rows, 1000):
            s.run(
                "UNWIND $rows AS r "
                "MERGE (m:Method {fqn: r.fqn}) "
                "SET m.name=r.name, m.sig=r.sig, m.return_type=r.return_type, "
                "    m.line=r.line, m.loc=r.loc, m.annotations=r.annotations, "
                "    m.body_snippet=r.body, m.javadoc=r.javadoc, m.file=r.file, "
                "    m.transactional=r.transactional, m.async=r.async_, "
                "    m.scheduled=r.scheduled, m.cacheable=r.cacheable "
                "WITH m, r MATCH (c:Class {fqn: r.class_fqn}) "
                "MERGE (c)-[:CONTAINS]->(m)",
                rows=chunk,
            )

        # PARAM_TYPE, RETURNS, THROWS
        pt_rows, rt_rows, th_rows = [], [], []
        for m in methods:
            src = m["fqn"]
            for i, (pt, pn) in enumerate(zip(m.get("param_types") or [],
                                             m.get("param_names") or [])):
                pt_rows.append({
                    "m": src, "c": resolve_class(pt, by_simple),
                    "pos": i, "name": pn,
                })
            rt = (m.get("return_type") or "").split("<")[0]
            if rt and rt != "void":
                rt_rows.append({"m": src, "c": resolve_class(rt, by_simple)})
            for th in m.get("throws") or []:
                th_rows.append({"m": src, "c": resolve_class(th, by_simple)})

        for chunk in batch(pt_rows, 1000):
            s.run(
                "UNWIND $rows AS r "
                "MATCH (m:Method {fqn: r.m}) "
                "MERGE (c:Class {fqn: r.c}) "
                "MERGE (m)-[pt:PARAM_TYPE {pos: r.pos}]->(c) "
                "SET pt.name = r.name",
                rows=chunk,
            )
        for chunk in batch(rt_rows, 1000):
            s.run(
                "UNWIND $rows AS r MATCH (m:Method {fqn: r.m}) "
                "MERGE (c:Class {fqn: r.c}) "
                "MERGE (m)-[:RETURNS]->(c)", rows=chunk)
        for chunk in batch(th_rows, 1000):
            s.run(
                "UNWIND $rows AS r MATCH (m:Method {fqn: r.m}) "
                "MERGE (c:Class {fqn: r.c}) "
                "MERGE (m)-[:THROWS]->(c)", rows=chunk)

        # Endpoints
        ep_rows = []
        for m in methods:
            ep = m.get("endpoint")
            if ep:
                ep_rows.append({
                    "m": m["fqn"], "http": ep["http"],
                    "path": ep["path"], "params": ", ".join(m.get("param_types") or [])[:400],
                })
        if ep_rows:
            s.run(
                "UNWIND $rows AS r "
                "MERGE (e:Endpoint {http: r.http, path: r.path}) "
                "SET e.params=r.params, e.method_fqn=r.m "
                "WITH e, r MATCH (m:Method {fqn: r.m}) "
                "MERGE (m)-[:EXPOSES]->(e)",
                rows=ep_rows,
            )

        # CALLS — use JavaParser's resolved FQN when available, else name+receiver type
        name_to_methods: dict[str, list[str]] = defaultdict(list)
        for m in methods:
            name_to_methods[m["name"]].append(m["fqn"])
        fields_by_class: dict[str, dict[str, str]] = {}
        for c in classes:
            fields_by_class[c["fqn"]] = {f["name"]: f["type"]
                                          for f in (c.get("fields") or [])}

        call_rows = []
        for m in methods:
            src = m["fqn"]
            klass_fqn = m["class_fqn"]
            flds = fields_by_class.get(klass_fqn, {})
            locals_ = m.get("locals") or {}
            for call in m.get("calls") or []:
                name = call.get("method")
                scope = call.get("scope") or ""
                resolved = call.get("resolved") or ""
                if not name:
                    continue
                # If JavaParser resolved it, match by class FQN + method name suffix.
                target = None
                if resolved:
                    # resolved looks like "com.foo.Bar.method(java.lang.String)"
                    # Find candidate method by class FQN prefix + method name.
                    prefix = resolved.rsplit(".", 1)[0]
                    for cand in name_to_methods.get(name, []):
                        if cand.startswith(prefix + "#"):
                            target = cand
                            break
                # Fall back to scope-based heuristic: if scope is a single
                # identifier we know the type of, resolve that class.
                if target is None:
                    recv = scope.strip().split(".")[-1]
                    recv_type = flds.get(recv) or locals_.get(recv)
                    if recv_type and recv_type in by_simple:
                        pref = by_simple[recv_type][0] + "#"
                        for cand in name_to_methods.get(name, []):
                            if cand.startswith(pref):
                                target = cand
                                break
                if target is None:
                    # last resort: first candidate with different fqn
                    for cand in name_to_methods.get(name, []):
                        if cand != src:
                            target = cand
                            break
                if target:
                    call_rows.append({
                        "a": src, "b": target,
                        "via": name,
                        "cert": "resolved" if resolved else "heuristic",
                    })

        for chunk in batch(call_rows, 2000):
            s.run(
                "UNWIND $rows AS r "
                "MATCH (a:Method {fqn: r.a}), (b:Method {fqn: r.b}) "
                "MERGE (a)-[c:CALLS {via: r.via}]->(b) "
                "SET c.certainty = r.cert",
                rows=chunk,
            )

        # MongoCollection R/W from body scans + repo field-type
        mongo_rows = []
        ext_rows = []
        nats_rows = []
        kafka_rows = []
        # Group methods by class so we can reuse class-level string constants
        # when resolving subscribe(SUBJECT_PATTERN) calls.
        methods_by_class: dict[str, list[dict]] = defaultdict(list)
        for m in methods:
            methods_by_class[m["class_fqn"]].append(m)
        class_body_cache: dict[str, str] = {}
        for m in methods:
            body = m.get("body") or ""
            src = m["fqn"]
            klass_fqn = m["class_fqn"]
            # Build class-level body blob once for const lookup.
            if klass_fqn not in class_body_cache:
                class_body_cache[klass_fqn] = "\n".join(
                    (mm.get("body") or "") for mm in methods_by_class[klass_fqn]
                )
            # mongoTemplate.* ops
            for match in _MONGO_TEMPLATE_RX.finditer(body):
                op = match.group(1)
                hint = None
                window = body[match.end(): match.end() + 300]
                cls_m = re.search(r"(\w+)\.class", window)
                if cls_m:
                    hint = collection_hints.get(cls_m.group(1)) or cls_m.group(1)
                if not hint:
                    continue
                if op.startswith(("find", "count", "exists")):
                    mongo_rows.append({"m": src, "c": hint, "op": "READS"})
                elif op.startswith(("delete", "remove")):
                    mongo_rows.append({"m": src, "c": hint, "op": "DELETES"})
                else:
                    mongo_rows.append({"m": src, "c": hint, "op": "WRITES"})
            # Spring Data repo calls
            flds = fields_by_class.get(klass_fqn, {})
            for call in m.get("calls") or []:
                scope = (call.get("scope") or "").strip()
                if not scope:
                    continue
                recv = scope.split(".")[-1]
                rtype = flds.get(recv)
                if not rtype:
                    continue
                if not (rtype.endswith("Repository") or rtype.endswith("Dao")):
                    continue
                hint = collection_hints.get(rtype)
                if not hint:
                    continue
                name = call.get("method") or ""
                if name.startswith(("find", "count", "exists", "get", "read")):
                    mongo_rows.append({"m": src, "c": hint, "op": "READS"})
                elif name.startswith(("delete", "remove")):
                    mongo_rows.append({"m": src, "c": hint, "op": "DELETES"})
                else:
                    mongo_rows.append({"m": src, "c": hint, "op": "WRITES"})
            # REST client URLs
            for u in _URL_RX.findall(body):
                if u.startswith("http") or u.startswith("/v1") or u.startswith("/api"):
                    ext_rows.append({"m": src, "u": u[:300]})
            # NATS publish / subscribe (literal)
            for sub in _NATS_PUB_RX.findall(body):
                nats_rows.append({"m": src, "s": sub, "op": "PUBLISH"})
            for sub in _NATS_SUB_RX.findall(body):
                nats_rows.append({"m": src, "s": sub, "op": "SUBSCRIBE"})
            # NATS subscribe via constant: resolve from class-level String
            # finals (emitted by the extractor) plus one-pass body scan.
            consts = {
                k: v for k, v in _STRING_CONST_RX.findall(class_body_cache[klass_fqn])
            }
            # Overlay constants declared as (:Class).fields[*].value
            for fld in classes_by_fqn.get(klass_fqn, {}).get("fields", []) or []:
                if isinstance(fld, dict) and fld.get("value") and fld.get("name"):
                    consts[fld["name"]] = fld["value"]
            for cname in _NATS_SUB_CONST_RX.findall(body):
                if cname in consts:
                    nats_rows.append({"m": src, "s": consts[cname], "op": "SUBSCRIBE"})
            # Kafka producer
            for topic in _KAFKA_PUB_RX.findall(body):
                kafka_rows.append({"m": src, "t": topic, "op": "PRODUCES"})
            for topic in _KAFKA_WRAP_RX.findall(body):
                kafka_rows.append({"m": src, "t": topic, "op": "PRODUCES"})
            # Kafka consumer (@KafkaListener on method). annotations_full carries
            # the full @KafkaListener(...) source text when the Extractor emits it.
            annotation_text = " ".join(m.get("annotations_full") or [])
            for ann_hit in _KAFKA_LISTENER_RX.finditer(annotation_text):
                if ann_hit.group(1):
                    kafka_rows.append({"m": src, "t": ann_hit.group(1), "op": "CONSUMES"})
                elif ann_hit.group(2):
                    for lit in re.findall(r'"([^"]+)"', ann_hit.group(2)):
                        kafka_rows.append({"m": src, "t": lit, "op": "CONSUMES"})

        # Dedup mongo_rows
        mongo_seen = set()
        mongo_dedup = []
        for r in mongo_rows:
            k = (r["m"], r["c"], r["op"])
            if k not in mongo_seen:
                mongo_seen.add(k)
                mongo_dedup.append(r)
        for op in ("READS", "WRITES", "DELETES"):
            subset = [r for r in mongo_dedup if r["op"] == op]
            if subset:
                for chunk in batch(subset, 1000):
                    s.run(
                        "UNWIND $rows AS r "
                        "MERGE (coll:MongoCollection {name: r.c}) "
                        "WITH coll, r MATCH (m:Method {fqn: r.m}) "
                        f"MERGE (m)-[:{op}]->(coll)",
                        rows=chunk,
                    )

        if ext_rows:
            s.run(
                "UNWIND $rows AS r MERGE (e:ExternalEndpoint {url: r.u}) "
                "WITH e, r MATCH (m:Method {fqn: r.m}) "
                "MERGE (m)-[:CALLS_EXTERNAL]->(e)",
                rows=ext_rows,
            )

        for op in ("PUBLISH", "SUBSCRIBE"):
            subset = [r for r in nats_rows if r["op"] == op]
            if subset:
                s.run(
                    "UNWIND $rows AS r MERGE (s:NatsSubject {subject: r.s}) "
                    "WITH s, r MATCH (m:Method {fqn: r.m}) "
                    f"MERGE (m)-[:{op}]->(s)",
                    rows=subset,
                )

        for op in ("PRODUCES", "CONSUMES"):
            subset = [r for r in kafka_rows if r["op"] == op]
            if subset:
                s.run(
                    "UNWIND $rows AS r MERGE (t:KafkaTopic {name: r.t}) "
                    "WITH t, r MATCH (m:Method {fqn: r.m}) "
                    f"MERGE (m)-[:{op}]->(t)",
                    rows=subset,
                )

    return len(classes), len(methods)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--neo4j", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    records = list(load_jsonl(Path(args.jsonl)))
    print(f"loaded {len(records)} records")
    hints = index_collection_hints(records)
    print(f"  {len(hints)} @Document collection hints")

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    if args.reset:
        with drv.session() as s:
            s.run("MATCH (n) DETACH DELETE n")
            print("  reset")

    nc, nm = write_graph(drv, records, hints)
    print(f"wrote {nc} classes, {nm} methods")
    with drv.session() as s:
        for l, c in s.run(
            "MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c ORDER BY c DESC"
        ).values():
            print(f"  {l}: {c}")
        print("---")
        for typ, c in s.run(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC"
        ).values():
            print(f"  {typ}: {c}")
    drv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
