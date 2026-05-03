"""Per-project standards / tools / tests catalogue.

Single source of truth for "what does dev activity look like in this
repo?" — used by every Doer tool that builds, tests, lints, formats,
or refactors. Replaces the patchwork of hardcoded ``mvn`` lines in
``ga_tools/{lint,tests,java_refactor}.py``.

Storage layout (KISS, DB-backed for durability + cross-machine
sync, with per-worktree YAML fallback):

1. **Neo4j ``:Repo``** node carries the canonical fields::

       (:Repo {
         name UNIQUE,
         lang, stack[], dockerfile, ports[], entry_cmd,
         build_cmd,    compile_cmd,    test_cmd,
         lint_cmd,     format_cmd,     security_scan_cmd,
         conventions[], forbidden_patterns[], env_vars[],
         acceptance_criteria[],
         updated_at
       })

2. **``.aiforge/aiforge.conf.yml``** in the worktree provides a
   per-tree override for ad-hoc experiments. Worktree YAML wins on
   conflict — operators iterate locally without touching the
   shared catalogue.

Public surface (KISS, four functions):
- ``get(repo_name, *, worktree=None) -> Standards``
- ``upsert(repo_name, **fields) -> Standards``
- ``render(std) -> str``
- ``apply_to_env(std)`` — lift ``lint_cmd`` / ``test_cmd`` / etc.
  into env so legacy ga_tools that read AIFORGE_LINT_CMD pick them
  up without code change.
"""
from __future__ import annotations

import glob as _glob
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable


@dataclass
class Standards:
    """Resolved per-project standards manifest."""
    name: str = ""
    lang: str = ""
    stack: list[str] = field(default_factory=list)
    dockerfile: bool = False
    ports: list[int] = field(default_factory=list)
    # Dev-activity commands (each may be empty → tool falls back to
    # its built-in default).
    entry_cmd: str = ""
    build_cmd: str = ""
    compile_cmd: str = ""
    test_cmd: str = ""
    lint_cmd: str = ""
    format_cmd: str = ""
    security_scan_cmd: str = ""
    # Quality + safety rails.
    conventions: list[str] = field(default_factory=list)
    forbidden_patterns: list[str] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    # Provenance.
    source: str = "default"  # 'neo4j' | 'worktree' | 'merged' | 'default'


# Sensible per-language defaults so brand-new repos still work
# without operator setup.
_DEFAULTS_BY_LANG: dict[str, dict[str, str]] = {
    "java": {
        "build_cmd":         "./mvnw clean package -DskipTests",
        "compile_cmd":       "mvn -q -DskipTests compile",
        "test_cmd":          "mvn test",
        "lint_cmd":          "mvn -q checkstyle:check",
        "format_cmd":        "mvn -q spotless:apply",
        "security_scan_cmd": "mvn -q org.owasp:dependency-check-maven:check",
    },
    "python": {
        "build_cmd":         "uv sync",
        "compile_cmd":       "python -m compileall -q .",
        "test_cmd":          "python -m pytest -q",
        "lint_cmd":          "ruff check .",
        "format_cmd":        "ruff format .",
        "security_scan_cmd": "bandit -q -r .",
    },
    "node": {
        "build_cmd":         "npm run build",
        "compile_cmd":       "tsc --noEmit",
        "test_cmd":          "npm test",
        "lint_cmd":          "npm run lint",
        "format_cmd":        "npx prettier --write .",
        "security_scan_cmd": "npm audit --audit-level=high",
    },
    "go": {
        "build_cmd":         "go build ./...",
        "compile_cmd":       "go build ./...",
        "test_cmd":          "go test ./...",
        "lint_cmd":          "go vet ./...",
        "format_cmd":        "gofmt -w .",
        "security_scan_cmd": "govulncheck ./...",
    },
    "react": {
        "build_cmd":         "yarn install && yarn build",
        "compile_cmd":       "yarn tsc --noEmit",
        "test_cmd":          "yarn test --watchAll=false",
        "lint_cmd":          "yarn lint",
        "format_cmd":        "yarn prettier --write src",
        "security_scan_cmd": "yarn audit --level high",
    },
}


def detect_lang(worktree_path: str) -> str:
    """Best-effort language fingerprint based on marker files in *worktree_path*.

    Detection rules (highest priority first):
      * ``pom.xml`` or any ``build.gradle*`` → ``"java"``
      * ``package.json``                     → ``"node"``
      * ``go.mod``                           → ``"go"``
      * ``pyproject.toml`` or ``requirements.txt`` → ``"python"``

    Returns ``""`` when no marker is found — callers should NOT then
    silently fall back to a Java toolchain. The Doer's pre-flight gate
    is wired to skip-with-warn instead.
    """
    if not worktree_path:
        return ""
    base = os.path.abspath(worktree_path)
    if not os.path.isdir(base):
        return ""
    if os.path.isfile(os.path.join(base, "pom.xml")):
        return "java"
    if _glob.glob(os.path.join(base, "build.gradle*")):
        return "java"
    if os.path.isfile(os.path.join(base, "package.json")):
        return "node"
    if os.path.isfile(os.path.join(base, "go.mod")):
        return "go"
    if (
        os.path.isfile(os.path.join(base, "pyproject.toml"))
        or os.path.isfile(os.path.join(base, "requirements.txt"))
    ):
        return "python"
    return ""


def get(repo_name: str, *, worktree: str | None = None) -> Standards:
    """Return the merged manifest for ``repo_name``.

    Resolution order (highest priority last):
      1. Per-language defaults (always present)
      2. Neo4j ``:Repo`` row (if catalog indexed)
      3. ``<worktree>/.aiforge/aiforge.conf.yml`` (operator override)
    """
    std = Standards(name=repo_name)
    _apply(std, _from_neo4j(repo_name))
    if worktree:
        _apply(std, _from_worktree(worktree))
    # Last-resort lang fallback: only fire when neither Neo4j nor the
    # worktree YAML supplied an explicit ``lang``. Don't override an
    # operator-set lang — that's the whole point of the override layer.
    if not (std.lang or "").strip() and worktree:
        guessed = detect_lang(worktree)
        if guessed:
            std.lang = guessed
            if std.source == "default":
                std.source = "auto-detect"
    _apply_defaults(std)
    if not std.source:
        std.source = "default"
    return std


def upsert(repo_name: str, **fields_to_update: Any) -> Standards:
    """Persist ``fields_to_update`` onto the ``:Repo`` node.

    Returns the freshly-loaded manifest. Best-effort: silently
    no-ops when Neo4j is unreachable.
    """
    if not repo_name.strip():
        raise ValueError("repo_name required")
    valid = {f.name for f in fields(Standards)} - {"source"}
    payload = {
        k: v for k, v in fields_to_update.items()
        if k in valid and v not in (None, "", [])
    }
    if not payload:
        return get(repo_name)
    try:
        _persist_to_neo4j(repo_name, payload)
    except Exception as exc:
        print(f"[repo_standards] persist failed: {exc}")
    return get(repo_name)


def render(std: Standards) -> str:
    """Compact prompt-friendly rendering."""
    parts = [f"[standards] {std.name} · lang={std.lang or '?'} · "
             f"source={std.source}"]
    for key in (
        "build_cmd", "compile_cmd", "test_cmd", "lint_cmd",
        "format_cmd", "security_scan_cmd", "entry_cmd",
    ):
        val = getattr(std, key)
        if val:
            parts.append(f"  {key:18s}= {val}")
    if std.conventions:
        parts.append(f"  conventions       = {len(std.conventions)} rules")
    if std.forbidden_patterns:
        parts.append(
            f"  forbidden         = {', '.join(std.forbidden_patterns[:5])}"
        )
    if std.acceptance_criteria:
        parts.append(
            f"  acceptance        = {len(std.acceptance_criteria)} item(s)"
        )
    return "\n".join(parts)


def apply_to_env(std: Standards) -> None:
    """Lift commands to env so legacy ga_tools see them.

    Mapping (manifest field → env var):
      build_cmd          → AIFORGE_BUILD_CMD
      compile_cmd        → AIFORGE_COMPILE_CMD
      test_cmd           → AIFORGE_TEST_CMD
      lint_cmd           → AIFORGE_LINT_CMD
      format_cmd         → AIFORGE_FORMAT_CMD
      security_scan_cmd  → AIFORGE_SECURITY_SCAN_CMD

    Existing env values WIN — operator pinning is preserved.
    """
    pairs = (
        ("build_cmd",         "AIFORGE_BUILD_CMD"),
        ("compile_cmd",       "AIFORGE_COMPILE_CMD"),
        ("test_cmd",          "AIFORGE_TEST_CMD"),
        ("lint_cmd",          "AIFORGE_LINT_CMD"),
        ("format_cmd",        "AIFORGE_FORMAT_CMD"),
        ("security_scan_cmd", "AIFORGE_SECURITY_SCAN_CMD"),
    )
    for attr, env in pairs:
        val = getattr(std, attr)
        if val and not os.environ.get(env):
            os.environ[env] = val


# ───────── helpers ────────────────────────────────────────────────


def _apply(std: Standards, src: dict | None) -> None:
    if not src:
        return
    valid = {f.name for f in fields(Standards)}
    for k, v in src.items():
        if k not in valid:
            continue
        if v in (None, "", []):
            continue
        setattr(std, k, v)
    if "source" in src:
        std.source = src["source"]


def _apply_defaults(std: Standards) -> None:
    lang_key = (std.lang or "").lower()
    if lang_key in _DEFAULTS_BY_LANG:
        for k, v in _DEFAULTS_BY_LANG[lang_key].items():
            if not getattr(std, k):
                setattr(std, k, v)


def _from_neo4j(repo_name: str) -> dict | None:
    try:
        from aiforge_core.legacy.rag.neo4j_memory import _get_driver
    except ImportError:
        return None
    cy = (
        "MATCH (r:Repo {name: $name}) "
        "RETURN r{.*} AS row LIMIT 1"
    )
    try:
        with _get_driver().session() as sess:
            rec = sess.run(cy, name=repo_name).single()
    except Exception as exc:
        print(f"[repo_standards] neo4j read failed: {exc}")
        return None
    if not rec:
        return None
    row = dict(rec["row"] or {})
    row["source"] = "neo4j"
    return _coerce(row)


def _from_worktree(worktree: str) -> dict | None:
    try:
        from aiforge_core.doer.ga_tools import repo_config as _rc
        cfg = _rc.load(worktree) or {}
    except Exception:
        cfg = {}
    if not cfg:
        return None
    cfg["source"] = "worktree"
    return _coerce(cfg)


def _coerce(row: dict) -> dict:
    """Whitelist + light type-cast so junk fields don't poison
    the dataclass hydrate."""
    valid = {f.name for f in fields(Standards)}
    out: dict = {}
    for k, v in row.items():
        if k not in valid:
            continue
        # Pretend "stack" / "ports" can arrive as a string ("java,maven").
        if k in ("stack", "conventions", "forbidden_patterns",
                 "env_vars", "acceptance_criteria") and isinstance(v, str):
            v = [s.strip() for s in v.split(",") if s.strip()]
        elif k == "ports" and isinstance(v, str):
            try:
                v = [int(p.strip()) for p in v.split(",") if p.strip()]
            except ValueError:
                v = []
        out[k] = v
    return out


def _persist_to_neo4j(repo_name: str, payload: dict) -> None:
    from aiforge_core.legacy.rag.neo4j_memory import _get_driver
    cy = (
        "MERGE (r:Repo {name: $name}) "
        "SET r += $payload, r.updated_at = datetime() "
        "RETURN r.name AS name"
    )
    with _get_driver().session() as sess:
        sess.run(cy, name=repo_name, payload=payload).consume()
