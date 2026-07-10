"""Mermaid flowchart → draw.io mxGraph conversion."""
from __future__ import annotations

from aiforge_core.runtime.tools import confluence_drawio as d


def test_parse_nodes_edges_subgraphs():
    m = d.parse_mermaid(
        'graph TB\n'
        '  subgraph "Data Centers"\n'
        '    DC1[DC 1]\n'
        '    DC2[DC 2]\n'
        '  end\n'
        '  DC1 -->|VPN Tunnel| V1[Vault Zone 1]\n'
        '  DC2 -.-> DB[(Databases)]\n')
    assert m is not None
    assert m.direction == "TB"
    assert m.nodes["DC1"]["label"] == "DC 1"          # real label kept
    assert m.nodes["DB"]["shape"] == "cylinder"
    assert m.nodes["V1"]["label"] == "Vault Zone 1"
    assert m.subgraphs[0]["title"] == "Data Centers"
    assert set(m.subgraphs[0]["members"]) == {"DC1", "DC2"}
    e = [x for x in m.edges if x["src"] == "DC1"][0]
    assert e["dst"] == "V1" and e["label"] == "VPN Tunnel"
    assert [x for x in m.edges if x["src"] == "DC2"][0]["dashed"] is True


def test_to_drawio_xml_shape():
    xml = d.to_drawio_xml(
        'flowchart LR\n A[Start] --> B{Choice}\n B -->|yes| C(Done)\n')
    assert xml.startswith("<mxfile")
    assert '<mxGraphModel' in xml and '<mxCell id="0"/>' in xml
    assert 'value="Start"' in xml and 'value="Choice"' in xml
    assert 'rhombus' in xml                            # {Choice} shape
    assert 'value="yes"' in xml                        # edge label
    assert 'edge="1"' in xml and 'vertex="1"' in xml


def test_non_flowchart_returns_none():
    assert d.to_drawio_xml("sequenceDiagram\n A->>B: hi") is None
    assert d.to_drawio_xml("just prose, no diagram") is None


def test_xml_escaping_and_cycle_safe():
    # labels with XML-special chars must be escaped; a cycle must not hang
    xml = d.to_drawio_xml('graph TD\n A[a & <b>] --> B\n B --> A\n')
    assert "&amp;" in xml and "&lt;b&gt;" in xml
    assert xml.count('vertex="1"') == 2
