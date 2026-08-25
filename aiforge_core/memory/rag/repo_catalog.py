"""(:Repo) catalog + auto-indexer.

Walks ``~/codeRepo/*`` and upserts a ``(:Repo)`` node per repo with
detected language, stack, entry command, and ports. Also writes a
parallel ``(:Memory {tier:'t2', wing:'repo/<name>'})`` row with the
same facts so Planner/Doer hit the catalog via ``search_memory`` even
when they don't know to call ``lookup_repo`` by name.

Schema::

    (:Repo {
        name UNIQUE, lang, stack[], entry_cmd, compile_cmd,
        ports[], dockerfile, readme_sha, last_seen_at
    })

Detection heuristics (fast, no subprocesses):
- ``pom.xml``                 → Java, parse `<java.version>` + parent ``spring-boot-*``
- ``requirements.txt`` / ``pyproject.toml`` → Python, parse frameworks
- ``package.json``            → Node, parse ``scripts.start`` / ``dev``
- README H2 ``## Run``        → take first fenced code block as ``entry_cmd``

The indexer is idempotent. Run via ``python -m aiforge_core.rag.repo_catalog``
or the ``com.aiforge.repo-indexer`` LaunchAgent (15-min interval).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aiforge_core.memory.rag.neo4j_memory import _get_driver, MemoryRow, retain_fact

_POM_XML = 'pom.xml'

log = logging.getLogger("aiforge.repo_catalog")

CODE_ROOT = Path(os.environ.get("AIFORGE_CODE_ROOT",
                                os.path.expanduser("~/codeRepo")))


@dataclass
class RepoFacts:
    name: str
    path: str
    lang: str = "unknown"
    stack: list[str] = field(default_factory=list)
    entry_cmd: str = ""
    compile_cmd: str = ""
    ports: list[int] = field(default_factory=list)
    dockerfile: bool = False
    readme_sha: str = ""
    overview: str = ""


# ─────────────── detectors ───────────────

_JAVA_VERSION_RX = re.compile(
    r"<(?:java\.version|maven\.compiler\.(?:source|target|release))>\s*"
    r"([^<]+?)\s*</"
)
_SPRING_BOOT_PARENT_RX = re.compile(
    r"<artifactId>\s*spring-boot-starter-parent\s*</artifactId>\s*"
    r"<version>\s*([^<]+?)\s*</version>",
    re.S,
)
# spring-boot-dependencies BOM in dependencyManagement — catches projects
# that use a multi-module parent but still import the BOM directly.
_SPRING_BOOT_BOM_RX = re.compile(
    r"<artifactId>\s*spring-boot-dependencies\s*</artifactId>\s*"
    r"<version>\s*([^<]+?)\s*</version>",
    re.S,
)
# Any spring-boot-starter-* dep proves Spring Boot is in play even when
# the version is inherited from a multi-module parent and thus absent
# from this pom.xml.
_SPRING_BOOT_STARTER_RX = re.compile(
    r"<artifactId>\s*spring-boot-starter[-\w]*\s*</artifactId>"
)
_WEBFLUX_RX = re.compile(r"spring-boot-starter-webflux")
_SPRING_BOOT_MAIN_RX = re.compile(
    r"@SpringBootApplication\b|SpringApplication\.run\("
)


def _find_spring_boot_main(repo: Path) -> bool:
    """Scan src/main for a @SpringBootApplication class."""
    src = repo / "src" / "main" / "java"
    if not src.is_dir():
        return False
    # Bounded walk: at most 200 files, 60KB each — cheap grep.
    count = 0
    for p in src.rglob("*.java"):
        count += 1
        if count > 200:
            break
        try:
            if _SPRING_BOOT_MAIN_RX.search(p.read_text(errors="ignore")[:60_000]):
                return True
        except Exception:
            continue
    return False


_PARENT_BLOCK_RX = re.compile(r"<parent>(.*?)</parent>", re.S)
# Trim in Python, not in the pattern: `\s*(lazy)\s*` lets the engine split
# the whitespace many ways (super-linear). `[^<]*` + .strip() is the same
# result, unambiguous, and says what it means.
_RELATIVE_PATH_RX = re.compile(r"<relativePath>([^<]*)</relativePath>")
_PARENT_ARTIFACT_RX = re.compile(r"<artifactId>([^<]*)</artifactId>")


def _parent_by_relative_path(cur: Path, parent_block: str) -> Path | None:
    """1. ``<relativePath>`` in the ``<parent>`` block (explicit path)."""
    rm = _RELATIVE_PATH_RX.search(parent_block)
    if not rm:
        return None
    cand = (cur.parent / rm.group(1).strip()).resolve()
    if cand.is_dir():
        cand = cand / _POM_XML
    return cand if cand.exists() else None


def _parent_by_default(cur: Path) -> Path | None:
    """2. Default Maven fallback ``../pom.xml`` (only when that file exists)."""
    cand = (cur.parent / ".." / _POM_XML).resolve()
    return cand if cand.exists() else None


def _parent_by_sibling_repo(parent_block: str, code_root: Path) -> Path | None:
    """3. Sibling repo by ``<artifactId>`` — ``~/codeRepo/<artifactId>/pom.xml``.

    Catches the OneShell layout where each service has its own repo and
    references ``oneshell-commons`` as parent without a relativePath.
    """
    am = _PARENT_ARTIFACT_RX.search(parent_block)
    if not am:
        return None
    sibling = code_root / am.group(1).strip() / _POM_XML
    return sibling.resolve() if sibling.exists() else None


def _next_parent_pom(cur: Path) -> Path | None:
    """The pom ``cur`` inherits from, by the three resolution rules in order."""
    try:
        txt = cur.read_text(errors="ignore")[:20_000]
    except Exception:  # noqa: BLE001
        return None
    pm = _PARENT_BLOCK_RX.search(txt)
    if not pm:
        return None
    block = pm.group(1)
    return (_parent_by_relative_path(cur, block)
            or _parent_by_default(cur)
            or _parent_by_sibling_repo(block, CODE_ROOT))


def _resolve_parent_pom(pom_path: Path) -> str:
    """Return merged XML from a pom and any local parent poms it references.

    Walks up the parent chain (max 3 hops) so services that inherit their
    Java version or spring-boot-starter-parent from an internal multi-module
    root get the right detection. See :func:`_next_parent_pom` for the
    per-hop resolution order.
    """
    chain = [pom_path]
    seen = {pom_path.resolve()}
    cur = pom_path
    for _ in range(3):
        nxt = _next_parent_pom(cur)
        if nxt is None or nxt in seen or not nxt.exists():
            break
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
    combined: list[str] = []
    for p in chain:
        try:
            combined.append(p.read_text(errors="ignore")[:40_000])
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(combined)


def _detect_java(repo: Path) -> tuple[list[str], str, str]:
    pom = repo / _POM_XML
    if not pom.exists():
        return [], "", ""
    try:
        xml_self = pom.read_text(errors="ignore")[:40_000]
    except Exception:
        return [], "", ""
    # Walk any local parent poms so versions inherited through a
    # multi-module root get found.
    xml = _resolve_parent_pom(pom)
    stack = ["Java"]
    jm = _JAVA_VERSION_RX.search(xml)
    if jm:
        stack[0] = f"Java {jm.group(1)}"

    # Spring Boot version resolution — check (in order): starter-parent,
    # dependencies BOM, then fall back to "starter-* present" detection.
    sm = _SPRING_BOOT_PARENT_RX.search(xml) or _SPRING_BOOT_BOM_RX.search(xml)
    sb_version = sm.group(1) if sm else ""
    has_starter = bool(_SPRING_BOOT_STARTER_RX.search(xml))
    if sb_version:
        label = "Spring Boot " + sb_version
        if _WEBFLUX_RX.search(xml):
            label += " WebFlux"
        stack.append(label)
    elif has_starter:
        # Version inherited from a multi-module parent we cannot see; still
        # label the service so Planner/Doer know it's a Spring Boot app.
        label = "Spring Boot"
        if _WEBFLUX_RX.search(xml):
            label += " WebFlux"
        stack.append(label)
    stack.append("Maven")

    compile_cmd = "mvn -q -DskipTests compile"
    # Library vs application: use the SELF pom for packaging — a parent
    # POM with packaging=pom is standard for multi-module parents, so the
    # merged xml always flags that. Only the self-pom tells us whether
    # THIS repo is a library or an app.
    packaging_pom = "<packaging>pom</packaging>" in xml_self
    is_app = bool(has_starter or sb_version) or _find_spring_boot_main(repo)
    is_lib = packaging_pom or not is_app
    entry_cmd = (
        "./mvnw clean install -DskipTests" if is_lib
        else "./mvnw spring-boot:run"
    )
    return stack, entry_cmd, compile_cmd


_PY_FW_MARKERS = {
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
    "uvicorn": "uvicorn",
    "streamlit": "Streamlit",
    "mcp": "MCP",
}


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text(errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def _python_entry(repo: Path) -> str:
    """main.py > app.py > manage.py (Django) > run.py."""
    for cand in ("main.py", "app.py", "manage.py", "run.py"):
        if (repo / cand).exists():
            return ("python manage.py runserver" if cand == "manage.py"
                    else f"python {cand}")
    return "python main.py"


def _detect_python(repo: Path) -> tuple[list[str], str, str]:
    req = repo / "requirements.txt"
    pyp = repo / "pyproject.toml"
    if not (req.exists() or pyp.exists()):
        return [], "", ""
    text = (_read_or_empty(req) if req.exists() else "")
    if pyp.exists():
        text += "\n" + _read_or_empty(pyp)
    tl = text.lower()
    stack = ["Python"]
    for key, label in _PY_FW_MARKERS.items():
        if key in tl and label not in stack:
            stack.append(label)
    stack.append("requirements.txt" if req.exists() else "pyproject.toml")
    install = ("pip install -r requirements.txt" if req.exists()
               else "pip install -e .")
    return (stack, f"{install} && {_python_entry(repo)}",
            "python -m compileall -q .")


def _detect_node(repo: Path) -> tuple[list[str], str, str]:
    pkg = repo / "package.json"
    if not pkg.exists():
        return [], "", ""
    try:
        obj = json.loads(pkg.read_text(errors="ignore"))
    except Exception:
        return [], "", ""
    stack = ["Node.js"]
    deps = {**(obj.get("dependencies") or {}),
            **(obj.get("devDependencies") or {})}
    for label in ("react", "next", "vite", "electron", "vue", "svelte",
                  "express", "fastify"):
        if label in deps:
            stack.append(label.capitalize())
    scripts = obj.get("scripts") or {}
    entry = scripts.get("dev") or scripts.get("start") or ""
    if entry:
        _script = "dev" if "dev" in scripts else "start"
        entry_cmd = f"npm install && npm run {_script}"
    else:
        entry_cmd = "npm install && npm start"
    return stack, entry_cmd, "npm install"


# ─────────────── README parsing ───────────────

_PORT_RX = re.compile(r"(?:port|:)\s*(\d{4,5})\b", re.I)
_H2_OVERVIEW_RX = re.compile(
    r"^##\s+Overview\s*\n+(.+?)(?=\n##\s|\Z)", re.M | re.S,
)


def _parse_readme(repo: Path) -> tuple[str, str, list[int]]:
    rd = repo / "README.md"
    if not rd.exists():
        return "", "", []
    try:
        text = rd.read_text(errors="ignore")
    except Exception:
        return "", "", []
    sha = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]
    overview = ""
    m = _H2_OVERVIEW_RX.search(text)
    if m:
        overview = m.group(1).strip()[:600]
    # Pull port mentions out of the first 6000 chars.
    ports = sorted({int(p) for p in _PORT_RX.findall(text[:6000])
                    if 1024 <= int(p) <= 65535})[:5]
    return sha, overview, ports


# ─────────────── walker ───────────────

def scan_repo(repo: Path) -> RepoFacts | None:
    if not repo.is_dir():
        return None
    facts = RepoFacts(name=repo.name, path=str(repo))

    for detect in (_detect_java, _detect_python, _detect_node):
        stack, entry, compile_cmd = detect(repo)
        if stack:
            facts.lang = stack[0].split()[0].lower()  # 'java' / 'python' / 'node.js'
            facts.stack = stack
            facts.entry_cmd = entry
            facts.compile_cmd = compile_cmd
            break

    if (repo / "Dockerfile").exists():
        facts.dockerfile = True
    facts.readme_sha, facts.overview, facts.ports = _parse_readme(repo)
    return facts


def scan_all(code_root: Path | None = None) -> list[RepoFacts]:
    root = code_root or CODE_ROOT
    out: list[RepoFacts] = []
    if not root.is_dir():
        log.warning("code root %s does not exist", root)
        return out
    for child in sorted(root.iterdir()):
        if child.name.startswith(".") or not child.is_dir():
            continue
        facts = scan_repo(child)
        if facts and facts.lang != "unknown":
            out.append(facts)
    return out


# ─────────────── writers ───────────────

def _ensure_schema() -> None:
    stmts = [
        "CREATE CONSTRAINT repo_name IF NOT EXISTS FOR (r:Repo) "
        "REQUIRE r.name IS UNIQUE",
        "CREATE INDEX repo_lang IF NOT EXISTS FOR (r:Repo) ON (r.lang)",
    ]
    with _get_driver().session() as s:
        for q in stmts:
            try:
                s.run(q)
            except Exception as exc:
                log.warning("repo schema init failed %r: %s", q[:60], exc)


def upsert_repo(facts: RepoFacts) -> None:
    params = {
        **asdict(facts),
        "last_seen_at": "timestamp()",  # placeholder; use Cypher timestamp()
    }
    with _get_driver().session() as s:
        s.run(
            "MERGE (r:Repo {name: $name}) "
            "SET r.path=$path, r.lang=$lang, r.stack=$stack, "
            "    r.entry_cmd=$entry_cmd, r.compile_cmd=$compile_cmd, "
            "    r.ports=$ports, r.dockerfile=$dockerfile, "
            "    r.readme_sha=$readme_sha, r.overview=$overview, "
            "    r.last_seen_at=timestamp()",
            **{k: v for k, v in params.items() if k != "last_seen_at"},
        )


def upsert_memory_twin(facts: RepoFacts) -> None:
    """Write a T2 canon memory that mirrors the repo fact so vector +
    BM25 retrieval surface it on queries like 'how do I run PosService'."""
    stack_line = ", ".join(facts.stack) if facts.stack else "unknown"
    ports_line = ", ".join(str(p) for p in facts.ports) if facts.ports else "n/a"
    docker_line = "yes" if facts.dockerfile else "no"
    text = (
        f"{facts.name} ({facts.lang}): {facts.overview or '(no overview)'}\n"
        f"Stack: {stack_line}\n"
        f"Ports: {ports_line}\n"
        f"Dockerfile: {docker_line}\n"
        f"Run: {facts.entry_cmd}\n"
        f"Compile gate: {facts.compile_cmd}"
    )
    retain_fact(MemoryRow(
        tier="t2",
        wing=f"repo/{facts.name}",
        kind="repo_fact",
        title=f"Repo catalog: {facts.name}",
        text=text,
        source="repo_catalog.indexer",
        metadata={"repo": facts.name, "lang": facts.lang,
                  "readme_sha": facts.readme_sha},
    ))


def run_once(code_root: Path | None = None) -> dict:
    _ensure_schema()
    facts_list = scan_all(code_root)
    for f in facts_list:
        try:
            upsert_repo(f)
            upsert_memory_twin(f)
        except Exception as exc:
            log.warning("upsert failed for %s: %s", f.name, exc)
    return {"indexed": len(facts_list),
            "names": [f.name for f in facts_list]}


# ─────────────── lookup (exposed to tools) ───────────────

def lookup_repo(name: str) -> dict | None:
    """Fetch a single Repo node by name. Returns None when unknown."""
    with _get_driver().session() as s:
        rec = s.run(
            "MATCH (r:Repo {name: $name}) "
            "RETURN r.name AS name, r.lang AS lang, r.stack AS stack, "
            "       r.entry_cmd AS entry_cmd, r.compile_cmd AS compile_cmd, "
            "       r.ports AS ports, r.dockerfile AS dockerfile, "
            "       r.overview AS overview, r.path AS path, "
            "       r.readme_sha AS readme_sha, r.last_seen_at AS last_seen_at",
            name=name,
        ).single()
        if rec is None:
            return None
        return dict(rec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    out = run_once()
    print(json.dumps(out, indent=2))
