# Graph-RAG (logic-aware RAG) for PosClientBackend

Build a Neo4j graph of the Java repo (classes, methods, endpoints, Mongo
reads/writes, calls, autowired deps, external REST targets, annotations)
and a vector layer on top. Ask natural-language questions; answer with
Cypher + cosine similarity in a single shot.

## Topology

```
Mac Studio 192.168.70.185   NUC 192.168.70.191 (static)    Laptop
--------------------------  -----------------------------  ------------------
LM Studio                   Neo4j 5.x (Docker)             dev + queries
  qwen3.6-35b-a3b @ 256K    JavaParser 3.26 + Maven
  qwen3-coder-next @ 256K   Python 3.12 / aiforge-venv
  nomic-embed-text (768d)   systemd --user lm-tunnel
                              -> Mac:1234 via ssh :1235
```

Laptop `bolt://192.168.70.191:7687` → NUC Neo4j. NUC reaches LM Studio
through a persistent ssh tunnel (systemd user unit `lm-tunnel.service`).

## Pipeline

```
repo/*.java
   │  JavaParser CLI (java -jar …/graph-rag-extractor.jar)
   ▼
/tmp/pcb-ast.jsonl  (one JSON per file)
   │  ingest_jsonl.py  (Python, Neo4j driver, batched UNWIND)
   ▼
Neo4j on NUC  (nodes + ~15 relationship types)
   │  embed_nodes.py  (per-node text -> nomic-embed -> 768-dim vector)
   ▼
Neo4j with vector indexes
   │  semantic.py / query.py
   ▼
Answers
```

## Schema (v3, JavaParser-backed)

### Nodes

| Label | Key properties |
|-------|-----------------|
| Package | name |
| Class | fqn, simple, kind, file, package, layer, loc, start_line, annotations, imports, javadoc, transactional, async, scheduled, cacheable, embedding |
| Method | fqn (`pkg.Class#method:line`), name, sig, return_type, line, loc, annotations, body_snippet (4 KB), javadoc, transactional, async, scheduled, cacheable, embedding |
| Endpoint | http, path, params, method_fqn |
| MongoCollection | name |
| NatsSubject | subject |
| ExternalEndpoint | url |
| Annotation | name |

### Edges

| Relation | From → To | Properties | Notes |
|----------|-----------|------------|-------|
| `CONTAINS_CLASS` | Package → Class | | |
| `CONTAINS` | Class → Method | | |
| `EXTENDS` | Class → Class | | supertypes, including library types |
| `IMPLEMENTS` | Class → Class | | |
| `USES` | Class → Class | field, final, inject | only @Autowired/@Inject/@Value/@Resource or final |
| `IMPORTS` | Class → Class | | only when imported class exists in the graph |
| `ANNOTATED` | Class or Method → Annotation | | 58 annotation types in PosClientBackend |
| `EXPOSES` | Method → Endpoint | | @*Mapping → full path |
| `CALLS` | Method → Method | via, certainty (`resolved` when JavaParser's symbol-solver gave a FQN, else `heuristic`) | |
| `PARAM_TYPE` | Method → Class | pos, name | one edge per parameter |
| `RETURNS` | Method → Class | | declared return type |
| `THROWS` | Method → Class | | checked exceptions |
| `READS` / `WRITES` / `DELETES` | Method → MongoCollection | | `mongoTemplate.*` + Spring Data repo methods |
| `CALLS_EXTERNAL` | Method → ExternalEndpoint | | WebClient.uri + URL literals in bodies |
| `PUBLISH` / `SUBSCRIBE` | Method → NatsSubject | | body scan |

### PosClientBackend v3 counts

```
Node                Count
Method              4,085
Class               1,362
Endpoint              463
Package               199
Annotation             58
MongoCollection        37
ExternalEndpoint       26

Relationship        Count
CALLS               8,503
PARAM_TYPE          8,117
CONTAINS            4,085
RETURNS             3,803
ANNOTATED           2,623
IMPORTS             2,183
USES                1,671
CONTAINS_CLASS      1,005
EXPOSES               463
EXTENDS               171
IMPLEMENTS            117
READS                  80
CALLS_EXTERNAL         31
WRITES                 17
THROWS                  7
```

## Commands

### One-time NUC setup

```bash
ssh mani@192.168.70.191

# repo
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew.git
python3 -m venv ~/aiforge-venv
~/aiforge-venv/bin/pip install neo4j httpx

# Neo4j (Docker, port 7474/7687)
docker run -d --name neo4j-aiforge --restart=unless-stopped \
    -p 7474:7474 -p 7687:7687 \
    -e NEO4J_AUTH=neo4j/password \
    -e 'NEO4J_PLUGINS=["apoc","genai"]' \
    -e NEO4J_dbms_memory_heap_max__size=4G \
    -v neo4j-data:/data neo4j:latest

# JavaParser jar
cd ~/AIForgeCrew/scripts/graph_rag/javaparser && mvn -q package -DskipTests

# Persistent SSH tunnel to LM Studio
systemctl --user enable --now lm-tunnel.service
```

### Ingest

```bash
# 1. mirror Java sources from laptop
rsync -az --exclude target --exclude .git \
    ~/Documents/codeRepo/PosClientBackend/src \
    ~/Documents/codeRepo/PosClientBackend/pom.xml \
    mani@192.168.70.191:~/code/PosClientBackend/

# 2. JavaParser extract (on NUC, ~4s)
ssh mani@192.168.70.191 'cd ~/code/PosClientBackend && \
    java -jar ~/AIForgeCrew/scripts/graph_rag/javaparser/target/graph-rag-extractor.jar \
        --repo $PWD --out /tmp/pcb-ast.jsonl'

# 3. push to Neo4j (on NUC, ~15s)
ssh mani@192.168.70.191 '~/aiforge-venv/bin/python \
    ~/AIForgeCrew/scripts/graph_rag/ingest_jsonl.py \
    --jsonl /tmp/pcb-ast.jsonl --reset'

# 4. embed (on NUC, ~90s via LM Studio tunnel)
ssh mani@192.168.70.191 '~/aiforge-venv/bin/python \
    ~/AIForgeCrew/scripts/graph_rag/embed_nodes.py \
    --lm http://127.0.0.1:1235/v1 --batch 32'
```

### Query (laptop or NUC)

```bash
# Structural canned queries
python scripts/graph_rag/query.py --neo4j bolt://192.168.70.191:7687 \
    --topic stockTransfer

# Semantic + 2-hop structural expansion
python scripts/graph_rag/semantic.py --neo4j bolt://192.168.70.191:7687 \
    --topk 5 --hops 2 "stock transfer complete validation"

# Ad-hoc Cypher
python scripts/graph_rag/query.py --neo4j bolt://192.168.70.191:7687 \
    --free 'MATCH (m:Method)-[:EXPOSES]->(e:Endpoint) WHERE e.path CONTAINS "stockTransfer" RETURN e, m LIMIT 20'
```

## Why Graph + Vector > just RAG

The Planner's earlier grep-based retrieval spent 15+ steps reading a
large controller before it could write a plan for a topic as simple as
"add pagination". With this graph:

- `MATCH (e:Endpoint {path:'…'})<-[:EXPOSES]-(m:Method)-[:CALLS*1..2]->(t)`
  returns the full call closure in one query.
- `MATCH (m:Method)-[:READS|WRITES]->(c:MongoCollection)` says which
  method mutates which collection without reading code.
- Vector similarity picks entry methods by intent even when the words
  are different ("*complete validation*" → the right three methods,
  none of which share the whole phrase in their name).
