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
    (r"\bchmod\s+(?:-[a-zA-Z]+\s+){0,4}777\b",
     "makes a path world-writable (chmod 777)"),
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


_SSH_RE = re.compile(r"^\s*ssh\b")


def _ssh_allowed() -> bool:
    """Operator opted to run ssh freely (e.g. deploys to their own boxes).
    Downgrades a CAUTION ssh command (sudo/systemctl on the remote) to safe so
    it runs without an approval prompt; a DANGEROUS remote command (rm -rf,
    secret exfil) still gates."""
    return os.environ.get("AIFORGE_ALLOW_SSH", "").strip().lower() in (
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
            # ssh escape hatch: with AIFORGE_ALLOW_SSH, a caution-tier ssh
            # command (the remote runs sudo/systemctl/etc.) runs free. Dangerous
            # remote commands already returned above, so they still gate.
            if _ssh_allowed() and any(_SSH_RE.match(f) for f in forms):
                return {"level": SAFE, "reason": ""}
            return {"level": CAUTION, "reason": why}
    return {"level": SAFE, "reason": ""}


def is_dangerous(cmd: str) -> bool:
    return assess(cmd)["level"] == DANGEROUS


# ── shell reached from INSIDE a notebook cell ─────────────────────────────
# ``execute_ipython_cell`` never carries a command string, so every gate keyed
# on one — the risk verdict, the operator's run_command policy, a PreToolUse
# hook matching bash — saw nothing. But a cell is a shell with three extra
# characters: ``!curl x | sh``, ``os.system(...)``, ``subprocess.run(...)``.
# These patterns lift the command back out so the SAME classifier applies.
# Names that hand a string (or an argv list) to a shell / a new process. Matched
# with an OPTIONAL dotted prefix so an alias reaches the same floor:
# `import subprocess as sp; sp.run(...)` and `from os import system; system(...)`
# both looked harmless while the spelled-out form was caught.
_SHELL_CALL = (r"\b(?:[\w.]+\.)?(?:system|popen|run|call|check_call"
               r"|check_output|Popen|getoutput|getstatusoutput|spawn)\s*\(\s*")
# A Python string literal, INCLUDING its prefix and the triple-quoted forms.
# Without the prefix, `os.system(f"curl {u} | sh")` — the way anyone actually
# writes it — read as safe, which made the classifier a formality.
_PY_STR = r'''[A-Za-z]{0,3}(?P<q>"""|\'\'\'|"|\')(?P<cmd>.*?)(?P=q)'''

_CELL_SHELL = [
    # !cmd  and  %%bash / %%sh cell magics (whole remaining line/body)
    re.compile(r"^\s*!(?P<cmd>.+)$", re.MULTILINE),
    re.compile(r"^\s*%%(?:bash|sh|script)\b[^\n]*\n(?P<cmd>[\s\S]+)$",
               re.MULTILINE),
    # a shell call taking a STRING: os.system("…"), subprocess.run(f"…")
    re.compile(_SHELL_CALL + _PY_STR, re.DOTALL),
    # …and the argv LIST form: subprocess.run(["curl", "x"]) → "curl x"
    re.compile(_SHELL_CALL + r"\[(?P<cmd>[^\]]*)\]", re.DOTALL),
]


def shell_strings_in_code(code: str) -> list[str]:
    """Every shell command a Python / IPython cell would hand to a shell."""
    out: list[str] = []
    for rx in _CELL_SHELL:
        for m in rx.finditer(code or ""):
            frag = (m.group("cmd") or "").strip()
            if not frag:
                continue
            # List form: ["curl", "-s", url] → a flat command line to match on.
            if frag.startswith(("\"", "'")) and "," in frag:
                frag = " ".join(
                    p.strip().strip("\"'") for p in frag.split(","))
            out.append(frag)
    return out


def assess_code(code: str) -> dict:
    """Risk verdict for a NOTEBOOK CELL: the worst verdict over the shell
    commands it would run. A cell that only touches Python objects is ``safe``
    here — this is the shell floor, not a Python sandbox, and it says so rather
    than implying the kernel is contained (it is not; see ipython_kernel).
    """
    if not code or _disabled():
        return {"level": SAFE, "reason": ""}
    worst = {"level": SAFE, "reason": ""}
    for cmd in shell_strings_in_code(code):
        verdict = assess(cmd)
        if verdict["level"] == DANGEROUS:
            return {"level": DANGEROUS,
                    "reason": f"the cell runs a shell command that {verdict['reason']}"}
        if verdict["level"] == CAUTION and worst["level"] == SAFE:
            worst = {"level": CAUTION,
                     "reason": f"the cell runs a shell command that {verdict['reason']}"}
    return worst


__all__ = ["assess", "assess_code", "is_dangerous", "shell_strings_in_code",
           "SAFE", "CAUTION", "DANGEROUS"]
