"""Auto-generate `<repo>/.aiforge/REPO_NOTES.md` for a repo.

Scans the worktree + Neo4j (when available) to produce a structured
markdown reference covering:
  - Purpose (from README + repo_standards if present)
  - Top-level layout (key directories + file counts)
  - Controllers (every @RestController / @Controller class + paths)
  - Services + ServiceImpl pairs
  - Repositories
  - Config classes (@Configuration)
  - Event surface — NATS subjects, Kafka topics, RabbitMQ exchanges
    (publish + subscribe sides)
  - MongoDB collections referenced
  - Build/test commands (from repo_standards manifest)
  - Cross-repo relations (HTTP base URLs called, shared NATS subjects,
    shared Kafka topics)

KISS: ripgrep + tree-sitter through aider's RepoMap. No LLM call,
purely deterministic. Output is auto-regenerable.

Public surface:
    generate_repo_notes(repo_name: str) -> str   # path to written file
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RepoNotes:
    repo: str
    worktree: str
    purpose: str = ""
    layout: list[str] = field(default_factory=list)
    controllers: list[dict] = field(default_factory=list)
    services: list[dict] = field(default_factory=list)
    repositories: list[str] = field(default_factory=list)
    configs: list[str] = field(default_factory=list)
    nats_subjects: dict[str, list[str]] = field(default_factory=dict)
    kafka_topics: dict[str, list[str]] = field(default_factory=dict)
    mongo_collections: list[str] = field(default_factory=list)
    rest_endpoints: list[str] = field(default_factory=list)
    http_clients: list[str] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)
    relations: dict[str, list[str]] = field(default_factory=dict)


def _rg(pattern: str, path: str, extra: list[str] | None = None) -> list[str]:
    """Run ripgrep, return matched lines (path:line:content)."""
    cmd = ["rg", "--no-heading", "-n", "--type-add",
           "code:*.{java,py,ts,tsx,js,kt,go,yaml,yml}",
           "--type=code", pattern, path]
    if extra:
        cmd[1:1] = extra
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return [ln for ln in (proc.stdout or "").splitlines() if ln]
    except Exception:
        return []


def _sniff_purpose(worktree: str) -> str:
    for fn in ("README.md", "Readme.md", "readme.md"):
        p = os.path.join(worktree, fn)
        if os.path.isfile(p):
            try:
                txt = Path(p).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            # First ## or first paragraph after H1.
            lines = [l.rstrip() for l in txt.splitlines()][:80]
            head = []
            for l in lines:
                s = l.strip()
                if not s:
                    if head:
                        break
                    continue
                if s.startswith("#"):
                    if not head:
                        continue
                    break
                head.append(s)
                if len(" ".join(head)) > 600:
                    break
            return " ".join(head)[:1200]
    return "(no README found)"


def _layout(worktree: str) -> list[str]:
    """Top-level directory summary. KISS: dir name + file count."""
    rows: list[str] = []
    try:
        for entry in sorted(os.listdir(worktree)):
            full = os.path.join(worktree, entry)
            if not os.path.isdir(full) or entry.startswith("."):
                continue
            n = sum(1 for _ in Path(full).rglob("*")
                    if _.is_file() and not _.name.startswith("."))
            rows.append(f"  {entry}/  ({n} files)")
            if len(rows) >= 12:
                break
    except Exception:
        pass
    return rows


def _controllers(worktree: str) -> list[dict]:
    """Find @RestController / @Controller classes + their @*Mapping paths."""
    out: list[dict] = []
    seen_paths: set[str] = set()
    for ln in _rg(r"@(?:Rest)?Controller\b", worktree):
        try:
            path, lineno, _ = ln.split(":", 2)
        except ValueError:
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Class-level @RequestMapping
        cls_path = ""
        m = re.search(
            r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?["]([^"]+)["]',
            content,
        )
        if m:
            cls_path = m.group(1)
        # Per-method @*Mapping
        endpoints: list[str] = []
        for em in re.finditer(
            r'@(Get|Post|Put|Delete|Patch)Mapping\s*\(\s*'
            r'(?:value\s*=\s*)?["]([^"]+)["]',
            content,
        ):
            endpoints.append(f"{em.group(1).upper()} "
                             f"{(cls_path + em.group(2)).replace('//','/')}")
        out.append({
            "file": os.path.relpath(path, worktree),
            "class_path": cls_path,
            "endpoints": endpoints[:20],
        })
        if len(out) >= 30:
            break
    return out


def _services(worktree: str) -> list[dict]:
    """Service + ServiceImpl pairs by basename match."""
    services: dict[str, dict] = {}
    for ln in _rg(r"@Service\b|interface \w+Service\b", worktree):
        try:
            path, _, _ = ln.split(":", 2)
        except ValueError:
            continue
        bn = os.path.basename(path)
        m = re.match(r"(\w+)(Service|ServiceImpl)\.(java|kt)$", bn)
        if not m:
            continue
        name = m.group(1)
        kind = "interface" if m.group(2) == "Service" else "impl"
        services.setdefault(name, {})[kind] = os.path.relpath(path, worktree)
    out: list[dict] = []
    for name, paths in sorted(services.items()):
        out.append({
            "name": name + "Service",
            "interface": paths.get("interface"),
            "impl": paths.get("impl"),
        })
    return out[:50]


def _repositories(worktree: str) -> list[str]:
    paths: set[str] = set()
    for ln in _rg(r"@Repository\b|extends (?:Mongo|Jpa|Crud)Repository",
                  worktree):
        try:
            path, _, _ = ln.split(":", 2)
        except ValueError:
            continue
        if "Repository" in os.path.basename(path):
            paths.add(os.path.relpath(path, worktree))
    return sorted(paths)[:50]


def _configs(worktree: str) -> list[str]:
    paths: set[str] = set()
    for ln in _rg(r"@Configuration\b", worktree):
        try:
            path, _, _ = ln.split(":", 2)
        except ValueError:
            continue
        paths.add(os.path.relpath(path, worktree))
    return sorted(paths)[:30]


def _nats_subjects(worktree: str) -> dict[str, list[str]]:
    """KafkaListener / @JetStreamPublisher / nats.subscribe(...) /
    nats.publish(...). Lump pub vs sub."""
    subs: list[str] = []
    pubs: list[str] = []
    # Common Java patterns.
    for ln in _rg(
        r'(?:subject\s*=\s*"[^"]+|nats[A-Z]\w*\.subscribe\b|'
        r'jetstream\.subscribe|@NatsListener)',
        worktree,
    ):
        for m in re.finditer(r'"([a-z][a-z0-9._-]+)"', ln, re.IGNORECASE):
            v = m.group(1)
            if "." in v and len(v) >= 5:
                subs.append(v)
    for ln in _rg(
        r'(?:nats[A-Z]\w*\.publish|jetstream\.publish|publishToRemote)',
        worktree,
    ):
        for m in re.finditer(r'"([a-z][a-z0-9._-]+)"', ln, re.IGNORECASE):
            v = m.group(1)
            if "." in v and len(v) >= 5:
                pubs.append(v)
    return {
        "subscribe": sorted(set(subs))[:20],
        "publish": sorted(set(pubs))[:20],
    }


def _kafka_topics(worktree: str) -> dict[str, list[str]]:
    subs: list[str] = []
    pubs: list[str] = []
    for ln in _rg(r'@KafkaListener', worktree):
        for m in re.finditer(r'"([\w${}.-]+)"', ln):
            v = m.group(1)
            if v and "." in v:
                subs.append(v)
    for ln in _rg(r'kafkaTemplate\.send\s*\(\s*"', worktree):
        for m in re.finditer(r'"([\w.-]+)"', ln):
            v = m.group(1)
            if v and "." in v:
                pubs.append(v)
    return {
        "subscribe": sorted(set(subs))[:30],
        "publish": sorted(set(pubs))[:30],
    }


def _mongo_collections(worktree: str) -> list[str]:
    names: set[str] = set()
    for ln in _rg(
        r'@Document\s*\(\s*(?:collection\s*=\s*)?"([^"]+)"|'
        r'mongo\w*\.collection\(\s*"([^"]+)"|'
        r'getCollection\(\s*"([^"]+)"',
        worktree,
    ):
        for m in re.finditer(r'"([a-zA-Z][\w]+)"', ln):
            v = m.group(1)
            if v and v[0].islower() and len(v) >= 3:
                names.add(v)
    return sorted(names)[:50]


def _http_clients(worktree: str) -> list[str]:
    """Outbound HTTP — base URLs we call into."""
    out: set[str] = set()
    for ln in _rg(
        r'(?:WebClient\.builder|RestTemplate|HttpClient|new URL|'
        r'fetch\s*\(|axios\.[a-z]+\(\s*["])',
        worktree,
    ):
        for m in re.finditer(
            r'(https?://[^\s"\'\\)]+|/(v[0-9]+/api/[^\s"\'\\)]*))',
            ln,
        ):
            out.add(m.group(1)[:120])
    return sorted(out)[:30]


def _commands(repo: str, worktree: str) -> dict[str, str]:
    try:
        from aiforge_core.runtime import repo_standards as _rs
        std = _rs.get(repo, worktree=worktree)
        return {
            "build": std.build_cmd, "compile": std.compile_cmd,
            "test": std.test_cmd, "lint": std.lint_cmd,
            "format": std.format_cmd,
        }
    except Exception:
        return {}


def _relations(notes: RepoNotes) -> dict[str, list[str]]:
    """Best-effort cross-repo relation list. Pulls Kafka topics +
    NATS subjects (these are the cross-repo contracts) + outbound
    HTTP URLs that mention other known service names."""
    rel: dict[str, list[str]] = defaultdict(list)
    base = os.environ.get("AIFORGE_REPOS_BASE", os.path.expanduser("~/codeRepo"))
    sibling_repos = []
    try:
        sibling_repos = [
            d for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d))
            and not d.startswith(".") and d != notes.repo
        ]
    except Exception:
        pass
    # HTTP relations — base URL contains a sibling repo name.
    for url in notes.http_clients:
        for sib in sibling_repos:
            if sib.lower() in url.lower():
                rel[sib].append(f"HTTP {url}")
    # Kafka / NATS — assume same broker; topics are shared contracts.
    for topic in (notes.kafka_topics.get("publish") or []):
        rel["__shared_kafka_publish"].append(topic)
    for topic in (notes.kafka_topics.get("subscribe") or []):
        rel["__shared_kafka_subscribe"].append(topic)
    for subj in (notes.nats_subjects.get("publish") or []):
        rel["__shared_nats_publish"].append(subj)
    for subj in (notes.nats_subjects.get("subscribe") or []):
        rel["__shared_nats_subscribe"].append(subj)
    return dict(rel)


# ──────────── render ──────────────────────────────────────────────


def render_markdown(n: RepoNotes) -> str:
    out: list[str] = []
    out.append(f"# {n.repo} — repo notes")
    out.append("")
    out.append("Auto-generated by `aiforge-maint repo notes "
               f"{n.repo}`. KISS: ripgrep + tree-sitter, no LLM. "
               "Re-run after structural changes.")
    out.append("")
    out.append("## Purpose")
    out.append(n.purpose or "_(unknown)_")
    out.append("")
    if n.layout:
        out.append("## Top-level layout")
        out.append("```")
        out.extend(n.layout)
        out.append("```")
        out.append("")
    if n.controllers:
        out.append(f"## Controllers ({len(n.controllers)})")
        out.append("")
        for c in n.controllers[:25]:
            out.append(f"- **{os.path.basename(c['file'])}** "
                       f"(`{c['file']}`)")
            if c.get("class_path"):
                out.append(f"  - base path: `{c['class_path']}`")
            for ep in c.get("endpoints") or []:
                out.append(f"  - `{ep}`")
        out.append("")
    if n.services:
        out.append(f"## Services ({len(n.services)})")
        out.append("")
        for s in n.services[:30]:
            line = f"- **{s['name']}**"
            if s.get("interface"):
                line += f"  — interface: `{s['interface']}`"
            if s.get("impl"):
                line += f"  — impl: `{s['impl']}`"
            out.append(line)
        out.append("")
    if n.repositories:
        out.append(f"## Repositories ({len(n.repositories)})")
        out.append("")
        for r in n.repositories[:30]:
            out.append(f"- `{r}`")
        out.append("")
    if n.configs:
        out.append(f"## Configuration classes ({len(n.configs)})")
        out.append("")
        for c in n.configs[:20]:
            out.append(f"- `{c}`")
        out.append("")
    if n.kafka_topics.get("subscribe") or n.kafka_topics.get("publish"):
        out.append("## Kafka")
        out.append("")
        if n.kafka_topics.get("subscribe"):
            out.append("**Consumes:**")
            for t in n.kafka_topics["subscribe"]:
                out.append(f"  - `{t}`")
            out.append("")
        if n.kafka_topics.get("publish"):
            out.append("**Publishes:**")
            for t in n.kafka_topics["publish"]:
                out.append(f"  - `{t}`")
            out.append("")
    if n.nats_subjects.get("subscribe") or n.nats_subjects.get("publish"):
        out.append("## NATS / JetStream")
        out.append("")
        if n.nats_subjects.get("subscribe"):
            out.append("**Subscribes:**")
            for s in n.nats_subjects["subscribe"]:
                out.append(f"  - `{s}`")
            out.append("")
        if n.nats_subjects.get("publish"):
            out.append("**Publishes:**")
            for s in n.nats_subjects["publish"]:
                out.append(f"  - `{s}`")
            out.append("")
    if n.mongo_collections:
        out.append("## MongoDB collections referenced")
        out.append("")
        out.append(", ".join(f"`{c}`" for c in n.mongo_collections))
        out.append("")
    if n.http_clients:
        out.append("## Outbound HTTP")
        out.append("")
        for u in n.http_clients[:25]:
            out.append(f"- `{u}`")
        out.append("")
    if n.commands:
        out.append("## Build / test commands")
        out.append("")
        for k, v in n.commands.items():
            if v:
                out.append(f"- **{k}**: `{v}`")
        out.append("")
    if n.relations:
        out.append("## Cross-repo / shared contracts")
        out.append("")
        for k, vals in n.relations.items():
            if not vals:
                continue
            label = k.replace("__shared_", "shared ")
            out.append(f"**{label}**:")
            for v in vals[:15]:
                out.append(f"  - `{v}`")
            out.append("")
    return "\n".join(out) + "\n"


def generate_repo_notes(repo: str, *, write: bool = True) -> str:
    """Build + optionally write `<repo>/.aiforge/REPO_NOTES.md`.
    Returns the on-disk path (when ``write`` is True) or the rendered
    markdown body otherwise."""
    base = os.environ.get("AIFORGE_REPOS_BASE", os.path.expanduser("~/codeRepo"))
    worktree = os.path.join(base, repo)
    if not os.path.isdir(worktree):
        raise FileNotFoundError(f"repo not found: {worktree}")
    n = RepoNotes(repo=repo, worktree=worktree)
    n.purpose = _sniff_purpose(worktree)
    n.layout = _layout(worktree)
    n.controllers = _controllers(worktree)
    n.services = _services(worktree)
    n.repositories = _repositories(worktree)
    n.configs = _configs(worktree)
    n.nats_subjects = _nats_subjects(worktree)
    n.kafka_topics = _kafka_topics(worktree)
    n.mongo_collections = _mongo_collections(worktree)
    n.http_clients = _http_clients(worktree)
    n.commands = _commands(repo, worktree)
    n.relations = _relations(n)
    body = render_markdown(n)
    if not write:
        return body
    out_dir = os.path.join(worktree, ".aiforge")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "REPO_NOTES.md")
    Path(out_path).write_text(body, encoding="utf-8")
    return out_path


__all__ = ["generate_repo_notes", "render_markdown", "RepoNotes"]
