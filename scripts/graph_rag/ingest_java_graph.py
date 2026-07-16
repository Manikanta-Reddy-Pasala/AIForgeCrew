"""Graph-RAG ingester v2 — Neo4j writer (split from ingest_java.py).

Schema DDL and the one-shot batched write_graph() that materialises the
ClassInfo/MethodInfo model into Neo4j. Byte-identical move — no behaviour change.
"""
from __future__ import annotations

from collections import defaultdict

from ingest_java_model import ClassInfo, MethodInfo


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
