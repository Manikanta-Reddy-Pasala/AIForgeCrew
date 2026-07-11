"""OKR node schema — typed frontmatter + markdown body, with edge extraction.

A node is one ``.md`` file: ``--- yaml --- \\n <markdown body>``. Four types
(objective / key_result / learning / session) with strongly-typed frontmatter
so a plain parser can build the DAG from the edges. Deterministic render (yaml
written by hand, JSON-encoded scalars) so files diff cleanly; tolerant parse
(yaml.safe_load) for hand edits. Nothing here raises on bad input.
"""
from __future__ import annotations

import datetime as _dt
import json
import re

NODE_TYPES = ("objective", "key_result", "learning", "session", "solution",
              "repo", "script", "task")

# id prefixes per type (session ids are date-based, handled in the store; a repo
# id is R-<workspace-slug> — one card per repo — set by the author, not counted).
_ID_PREFIX = {"objective": "O", "key_result": "KR", "learning": "L",
              "solution": "S", "repo": "R", "script": "SC", "task": "T"}

# Required + optional frontmatter keys per type (beyond `type` + `id`).
_SCHEMA: dict[str, dict] = {
    "objective": {"required": (), "opt": ("title", "status", "priority",
                                          "tags", "created_at")},
    "key_result": {"required": ("parent_objective",),
                   "opt": ("title", "status", "metrics")},
    "learning": {"required": ("scope",), "opt": ("title", "category")},
    "session": {"required": (), "opt": ("date", "linked_krs")},
    # A completed feature or bug fix — what was solved, and the entities it
    # touched (mapped to workspace/repo, topic, DB tables, connected services).
    "solution": {"required": ("kind",),
                 "opt": ("title", "description", "workspace", "topic",
                         "tables", "services", "files", "resource",
                         "timestamp", "about", "ticket", "scope")},
    # The canonical, detailed profile card for ONE repo (the hub) — how to
    # build/test/run it, where things live, how it deploys, what it connects to.
    "repo": {"required": ("workspace",),
             "opt": ("title", "stack", "build", "test", "run", "structure",
                     "entry_points", "deploy", "services", "tables", "gotchas",
                     "conventions", "scripts", "workflows", "scope",
                     "timestamp")},
    # A reusable shell / python script: what it does + how to run it.
    "script": {"required": ("name", "lang"),
               "opt": ("title", "purpose", "path", "run", "workspace",
                       "about", "scope", "timestamp")},
    # A small-task recipe — "how to do X in this repo" (steps in the body).
    "task": {"required": ("title",),
             "opt": ("workspace", "about", "tags", "scope", "timestamp")},
}

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _yaml_scalar(v) -> str:
    """A deterministic, YAML-valid scalar (JSON output is valid YAML)."""
    return json.dumps(v, ensure_ascii=False) if isinstance(v, str) else json.dumps(v)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def render_node(node_type: str, node_id: str, meta: dict, body: str = "") -> str:
    """Render one node → md text. ``meta`` holds the frontmatter fields (edges +
    attributes); ``type``/``id`` are stamped from the args. Lists render as YAML
    block sequences; scalars JSON-encoded. Unknown keys are preserved."""
    node_type = node_type if node_type in NODE_TYPES else "objective"
    fm: list[str] = ["---", f"type: {node_type}", f"id: {_yaml_scalar(node_id)}"]
    m = dict(meta or {})
    m.pop("type", None)
    m.pop("id", None)
    if node_type == "objective" and not m.get("created_at"):
        m["created_at"] = _now()
    for k, v in m.items():
        if isinstance(v, (list, tuple)):
            if v:
                fm.append(f"{k}:")
                fm.extend(f"  - {_yaml_scalar(x)}" for x in v)
            else:
                fm.append(f"{k}: []")
        elif isinstance(v, dict):
            fm.append(f"{k}:")
            fm.extend(f"  {kk}: {_yaml_scalar(vv)}" for kk, vv in v.items())
        elif v is not None:
            fm.append(f"{k}: {_yaml_scalar(v)}")
    fm.append("---")
    return "\n".join(fm) + "\n\n" + (body or "").strip("\n") + "\n"


def parse_node(text: str) -> dict:
    """Parse a node → ``{type, id, meta, body}``. ``meta`` is the full
    frontmatter dict (incl. type/id). Tolerant: a file without/with-broken
    frontmatter parses as ``{type:'', id:'', meta:{}, body:<all>}``."""
    text = text or ""
    meta: dict = {}
    m = _FRONTMATTER_RE.match(text)
    body = text
    if m:
        try:
            import yaml
            loaded = yaml.safe_load(m.group(1))
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:  # noqa: BLE001 — hand-edited yaml must not raise
            meta = {}
        body = text[m.end():]
    return {"type": str(meta.get("type") or ""), "id": str(meta.get("id") or ""),
            "meta": meta, "body": body.strip("\n")}


def edges_of(node: dict) -> list[tuple[str, str, str]]:
    """Directed edges out of a parsed node → ``(kind, src_id, dst_id)``.

    - key_result --parent--> objective   (``parent_objective``)
    - session    --covers--> key_result  (``linked_krs``)
    - learning   --scopes--> objective   (``scope`` / ``linked_objectives``)
    A ``scope: global`` learning yields no edge (it applies everywhere).
    """
    meta = node.get("meta") or {}
    nid = str(node.get("id") or "")
    out: list[tuple[str, str, str]] = []
    if not nid:
        return out
    po = meta.get("parent_objective")
    if isinstance(po, str) and po:
        out.append(("parent", nid, po))
    for kr in _as_list(meta.get("linked_krs")):
        out.append(("covers", nid, kr))
    scope = meta.get("scope")
    scopes = ([] if str(scope).lower() == "global" else _as_list(scope)) \
        + _as_list(meta.get("linked_objectives"))
    for oid in scopes:
        if str(oid).lower() != "global":
            out.append(("scopes", nid, str(oid)))
    return out


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return [str(v)] if str(v).strip() else []


def validate(node: dict) -> list[str]:
    """Return a list of schema problems (empty = valid). Soft — for a lint/UI,
    never raises."""
    problems: list[str] = []
    t = node.get("type")
    if t not in NODE_TYPES:
        return [f"unknown type {t!r}"]
    if not node.get("id"):
        problems.append("missing id")
    meta = node.get("meta") or {}
    for req in _SCHEMA[t]["required"]:
        if not meta.get(req):
            problems.append(f"{t} requires '{req}'")
    return problems


__all__ = ["NODE_TYPES", "render_node", "parse_node", "edges_of", "validate",
           "_ID_PREFIX"]
