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
    # network download piped into an interpreter — classic curl|bash RCE.
    # Match an interpreter ANYWHERE downstream of a pipe (so an intermediate
    # stage like ``curl x | tee a | sh`` is still caught, not just the first
    # pipe). Also catches process substitution ``bash <(curl ...)``.
    (r"\b(curl|wget|fetch)\b.*\|.*\b(sh|bash|zsh|ksh|python[0-9.]*|perl|ruby|node)\b",
     "pipes a network download into a shell/interpreter (remote code execution)"),
    (r"\b(sh|bash|zsh|ksh)\b\s+<\(\s*(curl|wget|fetch)\b",
     "executes a network download via process substitution (remote code execution)"),
    # secret EXFILTRATION: pushing creds/keys off the box (network/copy verbs)
    (r"(scp|curl|wget|rsync|nc|netcat|tar)\b[^\n]*(\.ssh/|\.aws/|\.env\b|id_rsa|id_ed25519|credentials|secrets?\b|\.pem\b|\.kube/|private[_-]?key)",
     "exfiltrates credentials / private keys / secrets off the machine"),
    (r"\benv\b[^\n]*\|\s*(curl|wget|nc|netcat)\b",
     "pipes the environment (likely secrets) to the network"),
    # raw disk / filesystem destruction
    (r"\bmkfs\b", "formats a filesystem"),
    (r">\s*/dev/(sd|nvme|disk|hd)", "writes over a raw disk device"),
    (r"\bdd\b[^\n]*\bof=/dev/", "dd writes to a raw device"),
    # fork bomb
    (r":\(\)\s*\{\s*:\s*\|\s*:", "fork bomb"),
    (r"\bshred\b", "irrecoverably shreds data"),
    # obfuscated payloads: decode-then-execute (base64/xxd → shell/interpreter).
    (r"\b(base64|xxd|openssl)\b[^\n]*\|[^\n]*\b(sh|bash|zsh|ksh|python[0-9.]*|perl|ruby|node)\b",
     "decodes an encoded payload and pipes it into a shell (obfuscated RCE)"),
    (r"\b(sh|bash|zsh|ksh)\b\s+-c\s+[\"']?\$\(", "runs a command-substitution payload via sh -c"),
]

# ── caution: reversible-ish but worth a confirmation under ask policy ─────
_CAUTION = [
    # local READ of creds/keys (no network) — surfaces secrets into the
    # agent's context; a heads-up, not an exfil.
    (r"\b(cat|less|more|grep|tail|head|cp)\b[^\n]*(\.ssh/|\.aws/|\.env\b|id_rsa|id_ed25519|credentials|secrets?\b|\.pem\b|\.kube/|private[_-]?key)",
     "reads credentials / private keys / secrets"),
    (r"\bsudo\b", "runs with elevated privileges (sudo)"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*777\b", "makes a path world-writable (chmod 777)"),
    (r"\bchown\b", "changes file ownership"),
    (r"\bgit\s+push\b[^\n]*--force(?!-with-lease)\b", "force-pushes (can clobber remote history)"),
    (r"\bgit\s+push\b[^\n]*\s-f\b", "force-pushes (can clobber remote history)"),
    # ANY push is a durable EXTERNAL action (updates a remote, may trigger CI /
    # a merge) — always worth a confirmation under ask policy, not just force.
    (r"\bgit\s+push\b", "pushes commits to a remote (external; updates the remote branch)"),
    (r"\bgh\s+pr\s+create\b", "opens a GitHub pull request"),
    (r"\bglab\s+mr\s+create\b", "opens a GitLab merge request"),
    (r"\b(npm|yarn|pnpm)\s+(install|add)\b[^\n]*\s-g\b", "installs a global package"),
    (r"\bpip[0-9.]*\s+install\b[^\n]*\s--user\b", "installs a user-wide Python package"),
    (r"\bkill(all)?\b\s+-9\b", "force-kills processes"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "stops/disables a system service"),
    (r"\bcrontab\b", "edits scheduled jobs"),
    (r"\biptables\b", "changes firewall rules"),
    (r"\beval\b", "eval of a constructed string (can hide a risky command)"),
]


def _normalize(cmd: str) -> str:
    """Defeat the cheap shell tricks that hide a token from a regex but that the
    shell strips before running: ``${IFS}`` word-splitting, empty quote pairs
    (``r''m``), and in-word backslashes (``r\\m``). Matching the normalized form
    AS WELL catches ``rm${IFS}-rf`` / ``c''url|sh`` etc. (Not bulletproof — a
    regex floor can't decode base64/chr(); pair with the sandbox/allowlist.)"""
    s = re.sub(r"\$\{IFS[^}]*\}", " ", cmd)
    s = re.sub(r"\$IFS\b", " ", s)
    s = s.replace("''", "").replace('""', "")
    s = re.sub(r"\\(?=[A-Za-z0-9])", "", s)
    return s

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
    # Match the raw AND the de-obfuscated form so quote/IFS/backslash tricks
    # can't smuggle a token past the regex.
    forms = (cmd, _normalize(cmd))
    for rx, why in _DANGEROUS_C:
        if any(rx.search(f) for f in forms):
            return {"level": DANGEROUS, "reason": why}
    if any(delete_guard.is_destructive_delete(f) for f in forms):
        return {"level": DANGEROUS, "reason": "deletes files/data"}
    for rx, why in _CAUTION_C:
        if any(rx.search(f) for f in forms):
            return {"level": CAUTION, "reason": why}
    return {"level": SAFE, "reason": ""}


def is_dangerous(cmd: str) -> bool:
    return assess(cmd)["level"] == DANGEROUS


__all__ = ["assess", "is_dangerous", "SAFE", "CAUTION", "DANGEROUS"]
