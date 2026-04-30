"""`aiforge-codemem` operator CLI.

Subcommands (plan 1 ships ingest, doctor, stats):
    aiforge-codemem ingest <repo> [--path DIR] [--force]
    aiforge-codemem doctor
    aiforge-codemem stats <repo>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from aiforge_core.codemem.ingest import flow
from aiforge_core.codemem.store import schema, state_db as sdb


def _driver():
    """Open the project's Neo4j driver. Errors propagate to caller."""
    from neo4j import GraphDatabase
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    return GraphDatabase.driver(uri, auth=(user, pw))


def _cmd_ingest(args: argparse.Namespace) -> int:
    drv = _driver()
    schema.apply(drv)
    state = sdb.open_db()
    sdb.migrate(state)
    res = flow.ingest_repo(
        repo_name=args.repo,
        repo_path=args.path or os.getcwd(),
        driver=drv,
        state_conn=state,
        force=args.force,
    )
    print(json.dumps({
        "status": res.status, "pack_sha": res.pack_sha, "repo": res.repo,
    }))
    return 0


def _cmd_services(args: argparse.Namespace) -> int:
    drv = _driver()
    with drv.session() as s:
        rows = list(s.run(
            "MATCH (r:Repo {name:$n})-[:OWNS_SERVICE]->(s:Service) "
            "OPTIONAL MATCH (s)-[:CONTAINS_FILE]->(f:File_v2) "
            "WITH s, count(f) AS file_count "
            "RETURN s.name AS name, s.role AS role, s.port AS port, "
            "       s.source AS source, s.tech_stack AS tech_stack, "
            "       file_count, s.description AS description "
            "ORDER BY s.name", n=args.repo,
        ))
    services = [dict(r) for r in rows]
    if not services:
        print(json.dumps({"repo": args.repo, "services": []}))
        return 0 if args.allow_empty else 1
    print(json.dumps({"repo": args.repo, "services": services}, indent=2))
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    drv = _driver()
    with drv.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:$n}) RETURN r", n=args.repo
        ).single()
    if not row:
        print(json.dumps({"error": "repo_not_found", "repo": args.repo}))
        return 1
    r = dict(row["r"])
    if "last_indexed_at" in r and r["last_indexed_at"] is not None:
        r["last_indexed_at"] = str(r["last_indexed_at"])
    runbook = r.pop("runbook_md", "") or ""
    conventions = r.pop("conventions_md", "") or ""
    r["runbook_md_chars"] = len(runbook)
    r["conventions_md_chars"] = len(conventions)
    print(json.dumps(r, indent=2, default=str))
    return 0


def _check_repomix() -> tuple[bool, str]:
    binary = os.environ.get("AIFORGE_CODEMEM_REPOMIX", "repomix")
    path = shutil.which(binary)
    if not path:
        return False, f"{binary} not on PATH"
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=5
        )
    except Exception as exc:
        return False, str(exc)
    return True, proc.stdout.strip() or "ok"


def _check_neo4j() -> tuple[bool, str]:
    try:
        drv = _driver()
        with drv.session() as s:
            s.run("RETURN 1").consume()
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def _check_llm() -> tuple[bool, str]:
    import urllib.error
    import urllib.request
    url = os.environ.get(
        "AIFORGE_CODEMEM_LM_URL",
        os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
    )
    probe = url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(probe, timeout=3) as resp:
            ok = resp.status == 200
        return (True, "ok") if ok else (False, f"status {resp.status}")
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks = [
        ("repomix", _check_repomix()),
        ("neo4j", _check_neo4j()),
        ("llm", _check_llm()),
    ]
    payload = {"checks": [{"name": n, "ok": ok, "info": info}
                          for n, (ok, info) in checks]}
    print(json.dumps(payload, indent=2))
    return 0 if all(ok for _, (ok, _) in checks) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aiforge-codemem")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="Stage 1+2 ingest of a repo")
    ing.add_argument("repo", help="Logical repo name (becomes Repo.name)")
    ing.add_argument("--path", help="Repo dir; defaults to CWD")
    ing.add_argument("--force", action="store_true",
                     help="Re-run even if pack_sha matches")
    ing.set_defaults(func=_cmd_ingest)

    st = sub.add_parser("stats", help="Print Repo node summary")
    st.add_argument("repo")
    st.set_defaults(func=_cmd_stats)

    sv = sub.add_parser("services", help="List services for a repo")
    sv.add_argument("repo")
    sv.add_argument("--allow-empty", action="store_true",
                    help="exit 0 even when no services found")
    sv.set_defaults(func=_cmd_services)

    doc = sub.add_parser("doctor", help="Check repomix, neo4j, llm")
    doc.set_defaults(func=_cmd_doctor)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
