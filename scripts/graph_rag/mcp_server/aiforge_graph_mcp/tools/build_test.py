"""Build / test plan tools. Emit commands in the order to run them."""
from __future__ import annotations

from ..cypher_lib import session


def build_plan(args: dict) -> dict:
    repo = args["repo"]
    cy = (
        "MATCH (r:Repo {name:$name}) "
        "RETURN r.lang AS lang, r.path AS path, "
        "r.build_install AS install, r.build_test AS test, "
        "r.build_package AS package, r.build_run_local AS run_local, "
        "r.image_prefix AS image, r.depends_on AS depends_on"
    )
    with session() as s:
        rec = s.run(cy, name=repo).single()
        if not rec:
            return {"error": f"repo not found: {repo}"}
        d = dict(rec)
    steps = []
    if d.get("install"):
        steps.append({"step": "install", "cmd": d["install"]})
    if d.get("test"):
        steps.append({"step": "test", "cmd": d["test"]})
    if d.get("package"):
        steps.append({"step": "package", "cmd": d["package"]})
    return {"repo": repo, "lang": d.get("lang"), "path": d.get("path"),
            "steps": steps, "depends_on": d.get("depends_on")}


def test_plan(args: dict) -> dict:
    """Given a set of method fqns OR changed files, return the tests that
    should be re-run."""
    fqns = args.get("fqns") or []
    files = args.get("files") or []

    cy = """
    UNWIND $fqns AS fqn
    MATCH (m) WHERE m.fqn = fqn
    OPTIONAL MATCH (t:Test)-[:TESTS]->(m)
    RETURN fqn AS target, collect(DISTINCT t.name) AS tests
    UNION
    UNWIND $files AS path
    MATCH (f:File {path: path})-[:DEFINES]->(s)
    OPTIONAL MATCH (t:Test)-[:TESTS]->(s)
    RETURN path AS target, collect(DISTINCT t.name) AS tests
    """
    with session() as s:
        return {"plan": [dict(r) for r in s.run(cy, fqns=fqns, files=files)]}


def run_commands(args: dict) -> dict:
    """Return local dev commands to bring a service up."""
    repo = args["repo"]
    cy = (
        "MATCH (r:Repo {name:$name}) "
        "RETURN r.build_run_local AS run_local, r.lang AS lang, r.path AS path, "
        "r.depends_on AS depends_on, r.env_required AS env_required"
    )
    with session() as s:
        rec = s.run(cy, name=repo).single()
        if not rec:
            return {"error": f"repo not found: {repo}"}
        return dict(rec)


TOOLS = [
    {
        "name": "build_plan",
        "description": "Return ordered build steps (install, test, package) for a repo.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    },
    {
        "name": "test_plan",
        "description": "Tests to re-run given a list of changed method fqns or file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fqns": {"type": "array", "items": {"type": "string"}},
                "files": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "run_commands",
        "description": "Return local-dev run commands + env vars needed for a repo.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    },
]

HANDLERS = {
    "build_plan": build_plan,
    "test_plan": test_plan,
    "run_commands": run_commands,
}
