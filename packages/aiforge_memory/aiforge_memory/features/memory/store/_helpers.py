"""Leaf constants + small pure helpers for the memory writer."""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable

_SCHEMA_VERSION = "codemem-v1"

_ALLOWED_LABELS = {"Decision_v2", "Observation_v2", "Note_v2", "Doc_v2"}


# ─── helpers ──────────────────────────────────────────────────────────

def _clamp01(x: float) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 1.0
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _text_hash(text: str) -> str:
    """Short sha256 of the observation text — indexed dedupe key
    (full text equality is re-verified at lookup time)."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _link_refs(
    session, *, repo: str, src_label: str, src_id: str, refs: Iterable[str],
) -> None:
    """Create MENTIONS edges from the memory node to existing
    Symbol_v2 (matched by fqname) or File_v2 (matched by path).

    A ref string with `::` is treated as a symbol fqname; otherwise as a
    file path. Missing targets are silently ignored — no placeholders."""
    for ref in refs:
        ref = (ref or "").strip()
        if not ref:
            continue
        if "::" in ref:
            cy = (
                f"MATCH (src:{src_label} {{id:$sid}}), "
                "(t:Symbol_v2 {repo:$repo, fqname:$ref}) "
                "MERGE (src)-[:MENTIONS]->(t)"
            )
        else:
            cy = (
                f"MATCH (src:{src_label} {{id:$sid}}), "
                "(t:File_v2 {repo:$repo, path:$ref}) "
                "MERGE (src)-[:MENTIONS]->(t)"
            )
        session.run(cy, sid=src_id, repo=repo, ref=ref).consume()
