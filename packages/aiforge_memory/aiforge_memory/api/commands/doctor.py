"""``aiforge-memory doctor`` — probe repomix + neo4j + llm.

Each check returns ``(ok: bool, info: str)``. The command exits 0 only
when every check passes — useful for systemd / launchd ``ExecStartPre``
gates so the scheduler doesn't start with a missing dependency.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess

from ._driver import driver


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
        drv = driver()
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


def run(args: argparse.Namespace) -> int:  # noqa: ARG001
    checks = [
        ("repomix", _check_repomix()),
        ("neo4j",   _check_neo4j()),
        ("llm",     _check_llm()),
    ]
    payload = {"checks": [{"name": n, "ok": ok, "info": info}
                          for n, (ok, info) in checks]}
    print(json.dumps(payload, indent=2))
    return 0 if all(ok for _, (ok, _) in checks) else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("doctor", help="Check repomix, neo4j, llm")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
