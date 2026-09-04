#!/usr/bin/env python3
"""Fail if the project carries ANY security finding on the Sonar server.

The count reached zero on 2026-09-04 — vulnerabilities, hotspots-to-review and
security rating A — and it got there by changing code rather than by marking
findings Won't Fix (the TLS opt-out that returned CERT_NONE, the hardcoded
http:// scheme checks, a SHA-1 used as an identity digest). Nothing keeps it
there. A single new ``verify=False`` in a hurry, or one more scheme literal,
puts it back to one, and nobody notices until the next time someone opens the
dashboard.

So: a check that says the number out loud, exits non-zero when it is not zero,
and names every finding it counted. Run it against any SonarQube:

    SONAR_HOST_URL=http://nuc:9010 SONAR_TOKEN=$(cat /tmp/sonartok) \\
        python scripts/sonar_security_gate.py

Exit codes: 0 clean · 1 findings present · 2 could not ask the server (a
scan that did not run is not a pass, and must not be reported as one).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PROJECT = "aiforgecrew"
TIMEOUT_S = 30


def _get(host: str, token: str, path: str, params: dict) -> dict:
    url = f"{host.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url)
    if token:
        # Sonar takes the token as the basic-auth USER with an empty password.
        import base64
        cred = base64.b64encode(f"{token}:".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _vulnerabilities(host: str, token: str, project: str) -> list[dict]:
    data = _get(host, token, "/api/issues/search",
                {"componentKeys": project, "resolved": "false",
                 "types": "VULNERABILITY", "ps": 100})
    return data.get("issues") or []


def _hotspots(host: str, token: str, project: str) -> list[dict]:
    data = _get(host, token, "/api/hotspots/search",
                {"projectKey": project, "status": "TO_REVIEW", "ps": 100})
    return data.get("hotspots") or []


def _line(issue: dict) -> str:
    comp = str(issue.get("component", "")).split(":")[-1]
    rule = issue.get("rule") or issue.get("ruleKey") or "?"
    return f"  {rule}  {comp}:{issue.get('line', '?')}  {issue.get('message', '')[:110]}"


def main() -> int:
    host = os.environ.get("SONAR_HOST_URL", "").strip()
    token = os.environ.get("SONAR_TOKEN", "").strip()
    project = os.environ.get("SONAR_PROJECT_KEY", DEFAULT_PROJECT).strip()
    if not host:
        print("SONAR_HOST_URL is not set — cannot check, and an unasked "
              "question is not a pass.", file=sys.stderr)
        return 2
    try:
        vulns = _vulnerabilities(host, token, project)
        spots = _hotspots(host, token, project)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"could not read {host}: {exc}", file=sys.stderr)
        return 2

    print(f"security findings on {project}: "
          f"{len(vulns)} vulnerabilities, {len(spots)} hotspots to review")
    for issue in vulns + spots:
        print(_line(issue))
    if vulns or spots:
        print("\nThis project's security count is 0 and is expected to stay 0. "
              "If a finding here is genuinely not a defect, the answer is still "
              "a code change or a documented decision in the review — not a "
              "silent Won't Fix.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
