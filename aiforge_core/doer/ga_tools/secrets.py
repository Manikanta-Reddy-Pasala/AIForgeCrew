"""Secret scanner — pre-commit gate against leaked credentials.

KISS: thin wrapper over ``gitleaks`` (preferred) or
``trufflehog`` (fallback). Run via the hooks pre_commit event.
Returns ``(found_count, summary)`` so the hooks dispatcher can
``block: true`` on positive hits.

Embedded heuristics layer covers the offline case (no scanner
installed): a single regex pass over ``git diff --staged`` for AWS
keys / private RSA / OAuth bearer / generic high-entropy strings.

Toggle:
- ``AIFORGE_DOER_SECRET_SCAN=0`` to disable entirely.
- ``AIFORGE_DOER_SECRET_SCAN=heuristic`` to skip external binaries.

Public surface:
- ``scan(worktree: str) -> tuple[int, str]``
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess


# Conservative regex set — false positives acceptable, missed
# positives are not. Mirrors gitleaks default ruleset for the
# top categories.
_PATTERNS = (
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws_secret",     re.compile(r"(?i)aws.{0,40}['\"][0-9a-zA-Z/+]{40}['\"]")),
    ("private_key",    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github_pat",     re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("github_oauth",   re.compile(r"gho_[A-Za-z0-9]{36}")),
    ("slack_token",    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,48}")),
    ("openai_key",     re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("anthropic_key",  re.compile(r"sk-ant-[A-Za-z0-9-]{20,}")),
    ("google_key",     re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("bearer_jwt",     re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")),
)


def scan(worktree: str) -> tuple[int, str]:
    """Scan the staged changes in ``worktree``. Returns
    ``(hit_count, summary)``."""
    if os.environ.get("AIFORGE_DOER_SECRET_SCAN", "1") == "0":
        return 0, "[secrets] scan disabled"

    forced = os.environ.get("AIFORGE_DOER_SECRET_SCAN", "").lower()
    if forced not in ("heuristic", "0"):
        if shutil.which("gitleaks"):
            return _scan_gitleaks(worktree)
        if shutil.which("trufflehog"):
            return _scan_trufflehog(worktree)
    return _scan_heuristic(worktree)


# ───────── implementations ──────────────────────────────────────────


def _scan_gitleaks(worktree: str) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            ["gitleaks", "protect", "--staged", "--no-banner",
             "--report-format", "json", "--report-path", "/dev/stdout"],
            cwd=worktree, capture_output=True, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired:
        return 0, "[secrets] gitleaks timeout"
    if cp.returncode == 0:
        return 0, "[secrets] gitleaks: clean"
    out = (cp.stdout or b"").decode("utf-8", "replace")
    return _count_findings(out), f"[secrets] gitleaks:\n{out[:1500]}"


def _scan_trufflehog(worktree: str) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            ["trufflehog", "git", "file://" + worktree,
             "--no-update", "--only-verified", "--json"],
            capture_output=True, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired:
        return 0, "[secrets] trufflehog timeout"
    out = (cp.stdout or b"").decode("utf-8", "replace")
    hits = sum(1 for line in out.splitlines() if line.strip())
    return hits, f"[secrets] trufflehog: {hits} verified hit(s)"


def _scan_heuristic(worktree: str) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            ["git", "diff", "--staged", "-U0"],
            cwd=worktree, capture_output=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        return 0, "[secrets] git-diff timeout"
    diff = (cp.stdout or b"").decode("utf-8", "replace")
    hits: list[str] = []
    for name, regex in _PATTERNS:
        for m in regex.finditer(diff):
            snippet = m.group(0)[:60]
            hits.append(f"  - {name}: {snippet}…")
            if len(hits) >= 20:
                break
        if len(hits) >= 20:
            break
    if not hits:
        return 0, "[secrets] heuristic: clean"
    return len(hits), "[secrets] heuristic findings:\n" + "\n".join(hits)


def _count_findings(json_blob: str) -> int:
    n = 0
    for line in json_blob.splitlines():
        line = line.strip()
        if line.startswith("{") and "RuleID" in line:
            n += 1
    return n
