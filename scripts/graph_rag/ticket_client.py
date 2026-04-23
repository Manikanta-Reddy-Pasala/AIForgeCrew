#!/usr/bin/env python3
"""Fetch ticket details from a custom provider (URL template in config).

Supports plain REST GET and templated POST (e.g. Linear GraphQL).
Never stores credentials; reads token from env var named in config.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

CFG = Path(__file__).parent / "config" / "ticket-url-template.yaml"


def _dotted(obj: Any, path: str):
    for part in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def fetch(ticket_id: str, provider: str = "default") -> dict:
    cfg = yaml.safe_load(CFG.read_text())
    p = (cfg.get("providers") or {}).get(provider)
    if not p:
        raise ValueError(f"unknown provider: {provider}")
    if not re.match(p["id_pattern"], ticket_id):
        raise ValueError(f"{ticket_id} fails pattern {p['id_pattern']}")

    headers = {"Accept": "application/json"}
    tok_env = p.get("api_auth_env")
    if tok_env and os.environ.get(tok_env):
        headers["Authorization"] = f"Bearer {os.environ[tok_env]}"

    method = p.get("method", "GET").upper()
    api_url = p["api_template"].format(id=ticket_id)

    if method == "GET":
        r = httpx.get(api_url, headers=headers, timeout=15)
    else:
        body = p.get("body_template", "{}").replace("{id}", ticket_id)
        r = httpx.post(api_url, headers=headers, content=body, timeout=15)
    r.raise_for_status()

    try:
        data = r.json()
    except json.JSONDecodeError:
        data = {}

    fields = {k: _dotted(data, path) for k, path in (p.get("fields") or {}).items()}
    fields["id"] = ticket_id
    fields["url"] = p["url_template"].format(id=ticket_id)
    fields["raw"] = data
    return fields


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticket_id")
    ap.add_argument("--provider", default="default")
    args = ap.parse_args()
    try:
        out = fetch(args.ticket_id, args.provider)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
