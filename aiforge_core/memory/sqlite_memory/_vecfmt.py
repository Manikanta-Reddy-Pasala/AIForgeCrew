"""How an embedding is stored in ``memory_units.embedding``.

Still JSON (every reader uses json.loads), but 6 significant digits and no
spaces. Python's default float repr wrote ~22 characters per dimension: on a
dev install the vectors were 7.8 MB of a 8.0 MB table, the text 0.24 MB. Six
digits are far beyond what cosine ranking can tell apart.
"""
from __future__ import annotations

import json


def vec_json(vec) -> str:
    return json.dumps([float(f"{float(x):.6g}") for x in (vec or [])], separators=(",", ":"))
