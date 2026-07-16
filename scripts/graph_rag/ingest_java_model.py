"""Graph-RAG ingester v2 — data model (split from ingest_java.py).

Dataclasses and layer-classification constants shared by the parse and graph
modules. Byte-identical move — no behaviour change.
"""
from __future__ import annotations

from dataclasses import dataclass, field


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
