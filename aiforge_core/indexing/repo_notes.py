"""Auto-generate `<repo>/.aiforge/REPO_NOTES.md` for a repo.

Scans the worktree to produce a structured markdown reference covering:
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


def _readme_lead(text: str) -> str:
    """The first paragraph after the H1 — the repo's own one-line purpose.

    Blank lines end the paragraph once it has started; heading lines are
    skipped before it and end it after.
    """
    head: list[str] = []
    for raw in text.splitlines()[:80]:
        line = raw.strip()
        if not line or line.startswith("#"):
            # Before the paragraph starts these are the H1 and its padding;
            # once it has started, either one ends it.
            if head:
                break
            continue
        head.append(line)
        if len(" ".join(head)) > 600:
            break
    return " ".join(head)[:1200]


def _sniff_purpose(worktree: str) -> str:
    for fn in ("README.md", "Readme.md", "readme.md"):
        p = os.path.join(worktree, fn)
        if not os.path.isfile(p):
            continue
        try:
            txt = Path(p).read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        return _readme_lead(txt)
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


_CLASS_MAPPING_RE = re.compile(
    r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?["]([^"]+)["]')
_METHOD_MAPPING_RE = re.compile(
    r'@(Get|Post|Put|Delete|Patch)Mapping\s*\(\s*(?:value\s*=\s*)?["]([^"]+)["]')


def _controller_endpoints(content: str, cls_path: str) -> list[str]:
    """``VERB /full/path`` for each @*Mapping method, under the class path."""
    return [f"{m.group(1).upper()} "
            f"{(cls_path + m.group(2)).replace('//', '/')}"
            for m in _METHOD_MAPPING_RE.finditer(content)]


def _controllers(worktree: str) -> list[dict]:
    """Find @RestController / @Controller classes + their @*Mapping paths."""
    out: list[dict] = []
    seen_paths: set[str] = set()
    for ln in _rg(r"@(?:Rest)?Controller\b", worktree):
        path = ln.split(":", 2)[0] if ln.count(":") >= 2 else ""
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        m = _CLASS_MAPPING_RE.search(content)
        cls_path = m.group(1) if m else ""
        out.append({
            "file": os.path.relpath(path, worktree),
            "class_path": cls_path,
            "endpoints": _controller_endpoints(content, cls_path)[:20],
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
            r'(https?://[^\s"\'\\)]+|/(v\d+/api/[^\s"\'\\)]*))',
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


def _sibling_repos(base: str, repo: str) -> list[str]:
    try:
        return [d for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d))
                and not d.startswith(".") and d != repo]
    except Exception:  # noqa: BLE001
        return []


def _relations(notes: RepoNotes) -> dict[str, list[str]]:
    """Best-effort cross-repo relation list. Pulls Kafka topics +
    NATS subjects (these are the cross-repo contracts) + outbound
    HTTP URLs that mention other known service names."""
    rel: dict[str, list[str]] = defaultdict(list)
    base = os.environ.get("AIFORGE_REPOS_BASE", os.path.expanduser("~/codeRepo"))
    # HTTP relations — base URL contains a sibling repo name.
    siblings = _sibling_repos(base, notes.repo)
    for url in notes.http_clients:
        for sib in siblings:
            if sib.lower() in url.lower():
                rel[sib].append(f"HTTP {url}")
    # Kafka / NATS — assume same broker; topics are shared contracts.
    for channels, prefix in ((notes.kafka_topics, "kafka"),
                             (notes.nats_subjects, "nats")):
        for direction in ("publish", "subscribe"):
            for name in (channels.get(direction) or []):
                rel[f"__shared_{prefix}_{direction}"].append(name)
    return dict(rel)


# ──────────── render ──────────────────────────────────────────────


def _okr_facts(n: RepoNotes) -> list[str]:
    """The measurable 'key results' of a scan — the counts that tell you at a
    glance what this repo exposes (the OKR ## Facts of a repo-notes note)."""
    f: list[str] = []
    if n.controllers:
        f.append(f"{len(n.controllers)} controllers")
    if n.services:
        f.append(f"{len(n.services)} services")
    if n.repositories:
        f.append(f"{len(n.repositories)} repositories")
    kp = len(n.kafka_topics.get("publish") or [])
    ks = len(n.kafka_topics.get("subscribe") or [])
    if kp or ks:
        f.append(f"Kafka: {kp} published / {ks} consumed topics")
    npub = len(n.nats_subjects.get("publish") or [])
    nsub = len(n.nats_subjects.get("subscribe") or [])
    if npub or nsub:
        f.append(f"NATS: {npub} published / {nsub} subscribed subjects")
    if n.mongo_collections:
        f.append(f"{len(n.mongo_collections)} MongoDB collections referenced")
    if n.http_clients:
        f.append(f"{len(n.http_clients)} outbound HTTP endpoints")
    return f


def _section(title: str, lines: list[str]) -> list[str]:
    """A ``## title`` block, or nothing when the section has no lines.

    Sections are separated by ONE blank line — a block whose lines already end
    with one (the pub/sub groups do) does not get a second.
    """
    if not lines:
        return []
    body = lines[:-1] if lines[-1] == "" else lines
    return [f"## {title}", "", *body, ""]


def _bullets(items, fmt=lambda v: f"- `{v}`") -> list[str]:
    return [fmt(v) for v in items]


def _controller_lines(controllers: list[dict]) -> list[str]:
    out: list[str] = []
    for c in controllers[:25]:
        out.append(f"- **{os.path.basename(c['file'])}** (`{c['file']}`)")
        if c.get("class_path"):
            out.append(f"  - base path: `{c['class_path']}`")
        out.extend(f"  - `{ep}`" for ep in (c.get("endpoints") or []))
    return out


def _service_lines(services: list[dict]) -> list[str]:
    out: list[str] = []
    for s in services[:30]:
        line = f"- **{s['name']}**"
        if s.get("interface"):
            line += f"  — interface: `{s['interface']}`"
        if s.get("impl"):
            line += f"  — impl: `{s['impl']}`"
        out.append(line)
    return out


def _pubsub_lines(channels: dict, sub_label: str) -> list[str]:
    """The consumes/publishes pair for one messaging surface."""
    out: list[str] = []
    for key, label in (("subscribe", sub_label), ("publish", "**Publishes:**")):
        values = channels.get(key)
        if values:
            out.append(label)
            out.extend(f"  - `{v}`" for v in values)
            out.append("")
    return out


def _relation_lines(relations: dict) -> list[str]:
    out: list[str] = []
    for k, vals in relations.items():
        if not vals:
            continue
        out.append(f"**{k.replace('__shared_', 'shared ')}**:")
        out.extend(f"  - `{v}`" for v in vals[:15])
        out.append("")
    return out


def _notes_body(n: RepoNotes) -> str:
    """The structured reference — one section per surface, each omitted when
    the scan found nothing for it."""
    out = [f"# {n.repo} — repo notes", "",
           "Auto-generated by `aiforge-maint repo notes "
           f"{n.repo}`. KISS: ripgrep + tree-sitter, no LLM. "
           "Re-run after structural changes.", "",
           "## Purpose", n.purpose or "_(unknown)_", ""]
    if n.layout:
        out += ["## Top-level layout", "```", *n.layout, "```", ""]
    out += _section(f"Controllers ({len(n.controllers)})",
                    _controller_lines(n.controllers))
    out += _section(f"Services ({len(n.services)})",
                    _service_lines(n.services))
    out += _section(f"Repositories ({len(n.repositories)})",
                    _bullets(n.repositories[:30]))
    out += _section(f"Configuration classes ({len(n.configs)})",
                    _bullets(n.configs[:20]))
    out += _section("Kafka", _pubsub_lines(n.kafka_topics, "**Consumes:**"))
    out += _section("NATS / JetStream",
                    _pubsub_lines(n.nats_subjects, "**Subscribes:**"))
    if n.mongo_collections:
        out += ["## MongoDB collections referenced", "",
                ", ".join(f"`{c}`" for c in n.mongo_collections), ""]
    out += _section("Outbound HTTP", _bullets(n.http_clients[:25]))
    if n.commands:
        # The header goes in whenever commands were DETECTED, even if every
        # value came back empty — an empty section says "we looked".
        out += ["## Build / test commands", "",
                *[f"- **{k}**: `{v}`" for k, v in n.commands.items() if v], ""]
    if n.relations:
        # Same rule as the commands section: the header records that the
        # relation scan RAN, even when every bucket came back empty.
        out += ["## Cross-repo / shared contracts", "",
                *_relation_lines(n.relations)]
    return "\n".join(out).strip("\n")


def render_markdown(n: RepoNotes) -> str:
    """Render the repo-notes body (the structured reference), then wrap it in
    the standard OKR note envelope (work_notes) so a repo-notes file carries
    the SAME frontmatter/Objective/Facts head as every other managed md —
    ``updated_at`` for staleness, deduped links, one parser everywhere."""
    # REPO_NOTES.md lives at <repo>/.aiforge/, OUTSIDE the work/<kind>/<key>/
    # tree, so a relative cross-ref md link would not resolve from here — the
    # cross-repo relations stay in the body's "Cross-repo / shared contracts"
    # section instead of the frontmatter links list.
    from aiforge_core.runtime import work_notes
    return work_notes.render_note(
        "repo", n.repo,
        title=f"{n.repo} — repo notes",
        objective=(f"Give agents a current structural map of {n.repo} — its "
                   "controllers, services, event surface and cross-repo "
                   "contracts — without an LLM scan each time."),
        key_results=_okr_facts(n),
        body_md=_notes_body(n))


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
