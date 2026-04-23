"""Regex-driven extraction of Mongo collections, NATS subjects, env vars."""
from __future__ import annotations

import re


_MONGO_COLL = re.compile(
    r"""(?:                       # matchers for collection name literals
      db\[[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\]
    | \.get_collection\([\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\)
    | database\[[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\]
    | \.collection\([\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\)
    )""",
    re.VERBOSE,
)

_NATS_PUB = re.compile(r"""\.publish\(\s*[\"']([a-zA-Z0-9_.\-\*]+)[\"']""")
_NATS_SUB = re.compile(r"""\.subscribe\(\s*[\"']([a-zA-Z0-9_.\-\*]+)[\"']""")

_ENV = re.compile(r"""os\.(?:environ\.get|environ|getenv)\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']""")


def detect_integrations(src: str) -> dict:
    mongo = set()
    for m in _MONGO_COLL.finditer(src):
        for g in m.groups():
            if g:
                mongo.add(g)
    nats_pub = {m.group(1) for m in _NATS_PUB.finditer(src)}
    nats_sub = {m.group(1) for m in _NATS_SUB.finditer(src)}
    env = {m.group(1) for m in _ENV.finditer(src)}
    return {
        "mongo": sorted(mongo),
        "nats_pub": sorted(nats_pub),
        "nats_sub": sorted(nats_sub),
        "env": sorted(env),
    }
