"""Shell-command risk classifier — broader than ``delete_guard``.

``delete_guard`` only catches destructive *deletes*. Frontier agents
(OpenHands' security analyzer, Cline/opencode permission prompts) gate a
wider set of risky actions: piping the network straight into a shell,
exfiltrating secrets, world-writable chmods, raw-disk writes, fork bombs,
privilege escalation. This module centralises that classification so both
the conversational chat agent (``run_command``) and the team Doer (``bash``)
can decide whether to run, ask, or refuse.

Levels (ascending):
  * ``safe``       — nothing matched; run autonomously.
  * ``caution``    — worth a heads-up / "ask" under an ``ask`` policy
                     (sudo, chmod 777, force-push, global installs).
  * ``dangerous``  — destructive or exfiltrating; refuse unless confirmed
                     (delete patterns, ``curl | sh``, secret exfil, mkfs,
                     fork bomb, raw-disk write).

Override the gate per-process with ``AIFORGE_RISK_DISABLE=1`` (treat all as
safe) — escape hatch for trusted batch runs.
"""
from __future__ import annotations

import os
import re

from . import delete_guard

# ── dangerous: destructive or exfiltrating — refuse unless confirmed ──────
_DANGEROUS = [
    # network piped straight into an interpreter (classic curl|bash installer
    # / remote-code-exec). Matches curl/wget/fetch … | sh|bash|zsh|python|…
    (r"\b(curl|wget|fetch)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|ksh|python[0-9.]*|perl|ruby|node)\b",
     "pipes a network download straight into a shell/interpreter (remote code execution)"),
    # secret exfiltration: reading creds/keys and sending them out
    (r"(cat|grep|tail|head|cp|scp|curl|tar)\b[^\n]*(\.ssh/|\.aws/|\.env\b|id_rsa|id_ed25519|credentials|secrets?\b|\.pem\b|\.kube/|private[_-]?key)",
     "touches credentials / private keys / secrets"),
    (r"\benv\b[^\n]*\|\s*(curl|wget|nc|netcat)\b",
     "pipes the environment (likely secrets) to the network"),
    # raw disk / filesystem destruction
    (r"\bmkfs\b", "formats a filesystem"),
    (r">\s*/dev/(sd|nvme|disk|hd)", "writes over a raw disk device"),
    (r"\bdd\b[^\n]*\bof=/dev/", "dd writes to a raw device"),
    # fork bomb
    (r":\(\)\s*\{\s*:\s*\|\s*:", "fork bomb"),
    (r"\bshred\b", "irrecoverably shreds data"),
]

# ── caution: reversible-ish but worth a confirmation under ask policy ─────
_CAUTION = [
    (r"\bsudo\b", "runs with elevated privileges (sudo)"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*777\b", "makes a path world-writable (chmod 777)"),
    (r"\bchown\b", "changes file ownership"),
    (r"\bgit\s+push\b[^\n]*--force(?!-with-lease)\b", "force-pushes (can clobber remote history)"),
    (r"\bgit\s+push\b[^\n]*\s-f\b", "force-pushes (can clobber remote history)"),
    (r"\b(npm|yarn|pnpm)\s+(install|add)\b[^\n]*\s-g\b", "installs a global package"),
    (r"\bpip[0-9.]*\s+install\b[^\n]*\s--user\b", "installs a user-wide Python package"),
    (r"\bkill(all)?\b\s+-9\b", "force-kills processes"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "stops/disables a system service"),
    (r"\bcrontab\b", "edits scheduled jobs"),
    (r"\biptables\b", "changes firewall rules"),
]

_DANGEROUS_C = [(re.compile(p, re.IGNORECASE), why) for p, why in _DANGEROUS]
_CAUTION_C = [(re.compile(p, re.IGNORECASE), why) for p, why in _CAUTION]

SAFE = "safe"
CAUTION = "caution"
DANGEROUS = "dangerous"


def _disabled() -> bool:
    return os.environ.get("AIFORGE_RISK_DISABLE", "").strip().lower() in (
        "1", "true", "yes", "on")


def assess(cmd: str) -> dict:
    """Classify ``cmd`` → ``{"level", "reason"}``.

    ``level`` ∈ {safe, caution, dangerous}. Deletes (from ``delete_guard``)
    are folded in as ``dangerous`` so callers get one risk verdict. Empty /
    disabled → safe.
    """
    if not cmd or _disabled():
        return {"level": SAFE, "reason": ""}
    for rx, why in _DANGEROUS_C:
        if rx.search(cmd):
            return {"level": DANGEROUS, "reason": why}
    if delete_guard.is_destructive_delete(cmd):
        return {"level": DANGEROUS, "reason": "deletes files/data"}
    for rx, why in _CAUTION_C:
        if rx.search(cmd):
            return {"level": CAUTION, "reason": why}
    return {"level": SAFE, "reason": ""}


def is_dangerous(cmd: str) -> bool:
    return assess(cmd)["level"] == DANGEROUS


__all__ = ["assess", "is_dangerous", "SAFE", "CAUTION", "DANGEROUS"]
