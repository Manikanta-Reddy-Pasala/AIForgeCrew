"""Fact Extract reflection runner.

Parses XML output from qwen3-4b-thinking into proposals queued into
memory_proposals (human-gated).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class Fact:
    kind: str
    text: str


@dataclass
class Recipe:
    title: str
    when: str
    how: str


@dataclass
class ReflectionResult:
    facts: list[Fact]
    recipes: list[Recipe]


def parse_reflection_xml(xml_text: str) -> ReflectionResult:
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return ReflectionResult(facts=[], recipes=[])

    facts: list[Fact] = []
    for f in root.findall("./facts/fact"):
        kind = f.attrib.get("kind", "fact")
        text = (f.text or "").strip()
        if text:
            facts.append(Fact(kind=kind, text=text[:300]))

    recipes: list[Recipe] = []
    for r in root.findall("./recipes/recipe"):
        title = r.attrib.get("title", "").strip() or "recipe"
        when_el = r.find("when")
        how_el = r.find("how")
        when = (when_el.text or "").strip() if when_el is not None else ""
        how = (how_el.text or "").strip() if how_el is not None else ""
        if how:
            recipes.append(Recipe(title=title[:80], when=when[:200], how=how[:500]))

    return ReflectionResult(facts=facts[:5], recipes=recipes[:3])


def submit_proposals(store, parent_id: str, result: ReflectionResult) -> list[int]:
    """Insert facts/recipes into memory_proposals. Returns new proposal IDs."""
    ids: list[int] = []
    for f in result.facts:
        ids.append(store.propose(
            tier="t2", wing="project", kind=f.kind,
            text=f.text, source_trace=parent_id, proposed_by="fact_extract",
        ))
    for r in result.recipes:
        body = f"WHEN: {r.when}\nHOW: {r.how}"
        ids.append(store.propose(
            tier="t3", wing="skills", kind="recipe", title=r.title,
            text=body, source_trace=parent_id, proposed_by="fact_extract",
        ))
    return ids
