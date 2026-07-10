"""Mermaid flowchart → draw.io (mxGraph) XML — so a ```mermaid block can be
published as a NATIVE, editable draw.io diagram in Confluence (the drawio
Confluence app renders an attached ``.drawio`` file, referenced by name; it does
not take mermaid inline).

Scope: the ``graph``/``flowchart`` subset that architecture diagrams use —
nodes with shapes, labelled/dashed/thick edges, and ``subgraph`` groups. Layout
is a simple layered placement (rank by longest path from a root); good enough to
render + be rearranged by hand in draw.io. Anything it can't parse falls back
(the caller keeps the raw fence as a code macro) — this never raises.

No timestamps / randomness (deterministic output → stable attachments, testable).
"""
from __future__ import annotations

import html
import re

# ── node shape syntaxes, longest-delimiter first so [( )] wins over [ ] ──────
_NODE_SHAPES = [
    ("cylinder", re.compile(r"^([A-Za-z0-9_]+)\[\((.*?)\)\]$")),   # [( )]
    ("stadium", re.compile(r"^([A-Za-z0-9_]+)\(\[(.*?)\]\)$")),    # ([ ])
    ("ellipse", re.compile(r"^([A-Za-z0-9_]+)\(\((.*?)\)\)$")),    # (( ))
    ("rhombus", re.compile(r"^([A-Za-z0-9_]+)\{(.*?)\}$")),        # { }
    ("rounded", re.compile(r"^([A-Za-z0-9_]+)\((.*?)\)$")),        # ( )
    ("rect", re.compile(r"^([A-Za-z0-9_]+)\[(.*?)\]$")),           # [ ]
]
_BARE_NODE = re.compile(r"^([A-Za-z0-9_]+)$")

# edge: SRC <link> [|label|] DST   — link carries arrow/dash/thick style
_EDGE_RE = re.compile(
    r"^(.*?)\s*(-{2,3}>|-\.->|={2,}>|-{3,}|-\.-)\s*(?:\|(.*?)\|\s*)?(.*)$")

_DIR_RE = re.compile(r"^(?:graph|flowchart)\s+(TB|TD|BT|LR|RL)\b", re.I)
_SUBGRAPH_RE = re.compile(r'^subgraph\s+(?:"([^"]*)"|(\S+))?\s*(?:\[(.*?)\])?', re.I)

_SHAPE_STYLE = {
    "rect": "rounded=0;whiteSpace=wrap;html=1;",
    "rounded": "rounded=1;whiteSpace=wrap;html=1;",
    "stadium": "rounded=1;arcSize=40;whiteSpace=wrap;html=1;",
    "cylinder": "shape=cylinder3;whiteSpace=wrap;html=1;boundedLbl=1;",
    "ellipse": "ellipse;whiteSpace=wrap;html=1;",
    "rhombus": "rhombus;whiteSpace=wrap;html=1;",
}
_NODE_W, _NODE_H = 140, 44
_HGAP, _VGAP = 60, 70
_PAD = 24                      # subgraph container padding around its members


def _esc(s: str) -> str:
    return html.escape(str(s or ""), quote=True)


def _parse_node_token(tok: str) -> tuple[str, str, str] | None:
    """A node token → (id, label, shape). None if it isn't a node ref."""
    tok = tok.strip()
    for shape, rx in _NODE_SHAPES:
        m = rx.match(tok)
        if m:
            return m.group(1), m.group(2).strip() or m.group(1), shape
    m = _BARE_NODE.match(tok)
    if m:
        return m.group(1), m.group(1), "rect"
    return None


class _Model:
    def __init__(self) -> None:
        self.direction = "TB"
        self.nodes: dict[str, dict] = {}        # id → {label, shape}
        self.edges: list[dict] = []             # {src, dst, label, dashed, thick, arrow}
        self.subgraphs: list[dict] = []         # {title, members:[id]}

    def node(self, nid: str, label: str | None = None, shape: str | None = None):
        n = self.nodes.setdefault(nid, {"label": nid, "shape": "rect"})
        # a REAL label (differs from the bare id) wins; a bare reference from an
        # edge never clobbers a label/shape set by the node's own declaration.
        if label and label != nid:
            n["label"] = label
        if shape and shape != "rect":
            n["shape"] = shape
        return nid


def parse_mermaid(src: str) -> _Model | None:
    """Parse a mermaid flowchart into a _Model. Returns None if it's not a
    flowchart (``graph``/``flowchart``) or has no nodes."""
    lines = [ln.strip() for ln in (src or "").splitlines()]
    if not any(re.match(r"^(graph|flowchart)\b", ln, re.I) for ln in lines):
        return None
    m = _Model()
    stack: list[dict] = []                       # open subgraphs
    for ln in lines:
        if not ln or ln.startswith("%%"):
            continue
        dm = _DIR_RE.match(ln)
        if dm:
            m.direction = dm.group(1).upper().replace("TD", "TB")
            continue
        if re.match(r"^subgraph\b", ln, re.I):
            sm = _SUBGRAPH_RE.match(ln)
            title = (sm.group(1) or sm.group(3) or sm.group(2) or "") if sm else ""
            sg = {"title": title.strip(), "members": []}
            m.subgraphs.append(sg)
            stack.append(sg)
            continue
        if ln.lower() == "end":
            if stack:
                stack.pop()
            continue
        em = _EDGE_RE.match(ln)
        if em and _parse_node_token(em.group(1)) and _parse_node_token(em.group(4)):
            s = _parse_node_token(em.group(1))
            d = _parse_node_token(em.group(4))
            m.node(s[0], s[1], s[2])
            m.node(d[0], d[1], d[2])
            link = em.group(2)
            m.edges.append({
                "src": s[0], "dst": d[0], "label": (em.group(3) or "").strip(),
                "dashed": link.startswith("-.") ,
                "thick": link.startswith("="),
                "arrow": link.endswith(">")})
            if stack:
                for nid in (s[0], d[0]):
                    if nid not in stack[-1]["members"]:
                        stack[-1]["members"].append(nid)
            continue
        # standalone node declaration
        nt = _parse_node_token(ln)
        if nt:
            m.node(nt[0], nt[1], nt[2])
            if stack and nt[0] not in stack[-1]["members"]:
                stack[-1]["members"].append(nt[0])
    return m if m.nodes else None


def _levels(m: _Model) -> dict[str, int]:
    """Longest-path rank from roots (nodes with no incoming edge)."""
    adj: dict[str, list[str]] = {n: [] for n in m.nodes}
    indeg: dict[str, int] = {n: 0 for n in m.nodes}
    for e in m.edges:
        if e["dst"] in adj and e["src"] in adj:
            adj[e["src"]].append(e["dst"])
            indeg[e["dst"]] += 1
    level = {n: 0 for n in m.nodes}
    # Kahn topological order; longest path so a node sits below all its parents.
    from collections import deque
    q = deque([n for n in m.nodes if indeg[n] == 0])
    seen = 0
    ind = dict(indeg)
    while q:
        u = q.popleft()
        seen += 1
        for v in adj[u]:
            level[v] = max(level[v], level[u] + 1)
            ind[v] -= 1
            if ind[v] == 0:
                q.append(v)
    # cycle fallback: any unranked node keeps level 0 (still renders)
    return level


def _layout(m: _Model) -> dict[str, tuple[int, int]]:
    """Assign (x, y) per node from its level + order within the level."""
    level = _levels(m)
    by_level: dict[int, list[str]] = {}
    for nid in m.nodes:                          # declaration order preserved
        by_level.setdefault(level[nid], []).append(nid)
    pos: dict[str, tuple[int, int]] = {}
    horizontal = m.direction in ("LR", "RL")
    for lv, ids in sorted(by_level.items()):
        for i, nid in enumerate(ids):
            a = lv * ((_NODE_W if horizontal else _NODE_H) + (_HGAP if horizontal else _VGAP))
            b = i * ((_NODE_H if horizontal else _NODE_W) + (_VGAP if horizontal else _HGAP))
            x, y = (a, b) if horizontal else (b, a)
            pos[nid] = (x + _PAD, y + _PAD)
    return pos


def to_drawio_xml(mermaid: str) -> str | None:
    """Mermaid flowchart → a draw.io ``.drawio`` (mxfile) XML string, or None if
    it can't be parsed as a flowchart. Never raises."""
    try:
        m = parse_mermaid(mermaid)
        if not m:
            return None
        pos = _layout(m)
        cells: list[str] = []
        # subgraph containers first (drawn behind the nodes they bound)
        for si, sg in enumerate(m.subgraphs):
            members = [n for n in sg["members"] if n in pos]
            if not members:
                continue
            xs = [pos[n][0] for n in members]
            ys = [pos[n][1] for n in members]
            gx, gy = min(xs) - _PAD, min(ys) - _PAD - 16
            gw = (max(xs) + _NODE_W + _PAD) - gx
            gh = (max(ys) + _NODE_H + _PAD) - gy
            style = ("rounded=0;whiteSpace=wrap;html=1;dashed=1;fillColor=none;"
                     "verticalAlign=top;fontStyle=1;")
            cells.append(
                f'<mxCell id="sg{si}" value="{_esc(sg["title"])}" style="{style}" '
                f'vertex="1" parent="1"><mxGeometry x="{gx}" y="{gy}" '
                f'width="{gw}" height="{gh}" as="geometry"/></mxCell>')
        # nodes
        for nid, n in m.nodes.items():
            x, y = pos.get(nid, (_PAD, _PAD))
            style = _SHAPE_STYLE.get(n["shape"], _SHAPE_STYLE["rect"])
            cells.append(
                f'<mxCell id="n_{_esc(nid)}" value="{_esc(n["label"])}" '
                f'style="{style}" vertex="1" parent="1"><mxGeometry x="{x}" '
                f'y="{y}" width="{_NODE_W}" height="{_NODE_H}" as="geometry"/></mxCell>')
        # edges
        for i, e in enumerate(m.edges):
            style = ["edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;"]
            if e["dashed"]:
                style.append("dashed=1;")
            if e["thick"]:
                style.append("strokeWidth=3;")
            if not e["arrow"]:
                style.append("endArrow=none;")
            cells.append(
                f'<mxCell id="e{i}" value="{_esc(e["label"])}" '
                f'style="{"".join(style)}" edge="1" parent="1" '
                f'source="n_{_esc(e["src"])}" target="n_{_esc(e["dst"])}">'
                f'<mxGeometry relative="1" as="geometry"/></mxCell>')
        body = "".join(cells)
        return (
            '<mxfile host="aiforge" type="device">'
            '<diagram id="aiforge-diagram" name="Page-1">'
            '<mxGraphModel dx="1024" dy="768" grid="1" gridSize="10" guides="1" '
            'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
            'math="0" shadow="0">'
            f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}</root>'
            '</mxGraphModel></diagram></mxfile>')
    except Exception:  # noqa: BLE001 — conversion is best-effort, never fatal
        return None


__all__ = ["to_drawio_xml", "parse_mermaid"]
